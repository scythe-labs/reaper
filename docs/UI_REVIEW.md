# UI/UX review: the whole frontend

- **Baseline:** `dev` @ `a7d7659`, reviewed 2026-07-24.
- **Method:** six parallel review agents swept disjoint slices of `frontend/src` (the review
  queue, the policy editor, settings/auth/setup, the API and shell layer, the reap and scan
  surfaces, and `index.css` plus docs/brand), each reading the backend route or service behind
  any claim that depended on server behavior. Findings that could not be tied to a concrete
  failure scenario were dropped at the agent. Roughly twenty of the highest-severity claims were
  then re-verified by hand against the code and the backend before landing here.
- **All CLAUDE.md gates pass at this baseline:** eslint clean, `tsc --noEmit` + `vite build`
  clean, 268/268 vitest. Every finding below is something a green build does not catch.
- **The standard applied** is the codebase's own system (the `index.css` control standard, the
  shared primitives, CLAUDE.md rules 17-21/36/39-51/52-69), not generic taste. Sanctioned
  exceptions are not flagged.
- **Severity totals:** 94 findings: 1 critical, 18 high, 45 medium, 30 low. By section: 5 security,
  36 bugs, 3 hacks, 9 refactors, 9 performance, 11 production readiness, 19 UI/UX consistency,
  2 improvements.

**What is genuinely good** (preserve it while fixing): the override overlay model
(`useOverrideMutations`, `_candidate_out`'s three views) is carefully reasoned and its comments are
honest about why; `DeletionToggle` and `SafetyBanner` model the correct unknown-state discipline;
`ReapBreakdown`'s `reapCount` already does the rule-62 subtraction the rest of its own page forgets;
the token system documents its contrast math in-file. Several findings below are "this file already
does it right, its sibling doesn't" — and in three cases the codebase has already written the fix
down in a comment somewhere else (B2, B13, U2). When a fix names a reference pattern, copy that
pattern, don't invent a new one.

## How to work this document

- Check off a finding (`[x]`) only after the fix passes the relevant gates
  (`npm --prefix frontend run lint`, `test`, `build`; the full CLAUDE.md gate set before a commit).
- Line numbers are exact at the baseline commit and will drift as fixes land: re-locate by the
  quoted class/identifier, not the number.
- Follow the **Agent Rules** at the bottom for every change, including changes unrelated to a
  listed finding.
- Suggested batch order is at the end. Findings sharing one mechanical pattern are cheapest fixed
  together.

## Progress (keep this current — it is the handoff between sessions)

**91 of 94 findings are checked off.** The fix order's seven batches are done (batch 7 left R1
and R2 unchecked, annotated in place with exactly what landed and what did not), batch 8 took the
four unscheduled findings that mislead or dead-end the operator, batch 9 the six that put a wrong
number or a wrong claim in front of them, and batch 10 the four cheap hygiene fixes. All are
committed on `ui-review-remediation`.

**Still open:** PR9, plus R1/R2's residue. One build-hygiene debt with no committed generator to
hang it on, and two restructures with no behavior change. Nothing here is operator-visible; each
is annotated in place with why it was left.

**Run the gates with `set -o pipefail`.** Piping a step into `tail` returns `tail`'s status:
batch 8 found `tsc` failing under a chain that reported success.

- **Batch 1, the deletion path:** S1, B3, B4, B5, B15, B16, B25, PR1, PR8, PR10.
- **Batch 2, the queue's control grammar:** B1, B2, B12, B24, B13, B36, U13. Each of the four
  behavioral fixes carries a test proven to fail without it (reverted one at a time, ran the
  single test, restored). Driven live in Chrome: the active masthead tab and the active lane
  keep their raised pill under the pointer (B13), Enter on a focused season pill expands the
  show instead of opening its panel (B2), and the bulk bar reads "1 card · 3 items" against a
  card whose own line says "3 of 6 would be removed" (U13). B36 is the same CSS mechanism as
  B13 on the docs index, verified by the fix, not by a screen — the docs modal's entry point
  was not found from the masthead or the user menu.

What the next session should know:

- **Live pass, partial.** Driven on the real dev app: the Reap page's ledger and split agree
  (funnel arithmetic and the movie/season split both resolve to the same total), a history plan
  loads through the new `["run", id]` query with its staleness warning, Execute, and step table
  intact, and the console is clean. Not driveable without arming deletion or deleting real
  files, so left to component tests: S1's typed gate, B3's error state, B4's invalidation, and
  PR8's incomplete-scan notice. **Never execute a real reap to test one of these.**
- **Shape changes other batches will touch.** `api.createRun` now takes `"all" | string[]` (an
  empty array throws); `useHoldsBackUnmeasured()` now returns `{holdsBack, isPending, isError}`
  (P7 will want to hoist it out of the rows); `ReapPlan` holds `runId` and reads the run through
  `useQuery(["run", runId])`, so anything that mutates a run must invalidate `["run"]`, not only
  `["runs"]`. The always-mounted `ReapBar` in `App.tsx` now owns post-reap cache invalidation.
- **Batch 2's shape changes.** `ShowCard` derives `isReapTab` from `show_override` FIRST and
  feeds the same value to `CardStatusLine`, so a whole-show decision moves the tint, the chip,
  the meta clause, and the status line together. `CardStatusLine` falls back to the dormancy
  pill when a non-condemned row has no chip. The bulk bar's count now needs
  `group_condemned_count`, so a change to that field changes a number beside a destructive
  button.
- **Still open in the same area:** U10's wording sits in `ReapBreakdown`'s held-reaps line and is
  batch 6; P3's `_run_out` cost and R1's split of `ReviewQueue.tsx` are batch 7. None touched.

### Batch 3, the number-input family (B7, B8, U17, PR5, PR6)

One mechanism fixed all of B7 and B8: `useTypedNumber` in `components/QuantityInput.tsx` is now
the one place a number lives while it is being typed. While a box has focus it shows the text
that was typed and nothing else; the parent hears only text that actually parses, so an empty
or half-finished box ("", "7.") says nothing at all; blur is the commit, and pulls an
out-of-range number into range. Both `QuantityInput` and `FixedQuantity` use it, and it is
exported for the two unitless ramp-bound boxes in `PolicyEditor` that carried the same
`Number(e.target.value) || 0` coercion (not a listed finding -- same defect, same file, fixed
rather than left as a second answer to one question).

- **The re-floors the review named are gone**, since they existed only to paper over the
  coercion: `max_items_per_run: v || 1`, `max_items_per_30d: v || 1`, and the people
  threshold's `gate.threshold || 1` on both the value and the suffix. The remaining
  `Math.max(0, v)` calls are left alone: they are unreachable now, but they are not lies.
- **U17** re-derives the display unit when the value is replaced from outside, and remembers
  the box's own emits in a ref so typing a fraction never jumps the dropdown mid-keystroke.
  Both halves have a test; the guard test fails against the naive always-re-derive version.
- **PR5** put `max={1000}` / `max={25}` on the pace boxes and the bounds in the help text --
  which exposed that `.qty-narrow` at 3.6rem **clips a 4-digit number**. The matrix's narrow
  number track is now 4.3rem (3.7rem under 640px) with a comment saying why. A ceiling you can
  reach but not read is worse than no ceiling; if you add a `max` to a narrow box, check it fits.
- **PR6** capped the credential fields at the server's own `max_length` (128, and 256 for the
  recovery code) in `Login` (LocalSheet + RecoveryCard), `DeletionToggle`, and Settings'
  `AdminPasswordForm` + `RestoreCard`.

Verification: 9 new tests in the new `components/QuantityInput.test.tsx`. Each was reverted
against and re-run -- all 7 `FixedQuantity` cases fail without the buffer, U17's fails without
the ref, and the "holds still" guard fails against the naive fix. **Note for whoever writes the
next number-box test:** jsdom + userEvent do *not* reproduce the caret-append, so the two
headline cases pass either way until they also assert the box never emitted the phantom `0`.
That zero is the actual defect; assert on it.

Driven live in Chrome on the real app, then discarded (nothing was saved): clearing "Most
titles per run" leaves it genuinely empty and typing 25 stores **25, not 125**; 9999 commits
as 1000 and now reads in full; typing "8." in the IMDb bar leaves the sentence beneath it at
"at least **8.0**" (never the 0.0 that would clear every title) and lands on 8.2; a grace box
parked on "years" showing 0.02 returns to "**2 weeks**" on Discard; all three password fields
report `maxLength: 128`. Console clean.

### Batch 4, the unknown-state and stale-cache sweep (B9, B14, B21, B22, PR2, PR3, PR4, B20, B19, B18)

Ten findings that all shared one shape: a surface that could not tell "nothing" from "we could
not look", or a cache that nobody refreshed.

- **B9** moved the `["safety"]` query into a shared **`useSafety()`** hook (`src/useSafety.ts`)
  and gave it `refetchInterval: 15000` + `refetchOnWindowFocus: true`. All six call sites use it
  (`App`, `SecurityPanel`, `ReapPlan`, `ReapConfirm`, `DeletionToggle`), so the polling lives in
  one declaration instead of six. Worth knowing: **React Query holds the interval while the tab
  is hidden**, so the two settings cover each other -- a background tab costs nothing and
  refetches on return, a visible tab runs the clock. The hook's comment says so; keep it true.
- **B14** replaced the two hand-copied invalidation lists in `useOverrideMutations` with one
  `OVERRIDE_AWARE` constant that now also carries `["snapshot"]` and `["fairness"]`. A surface
  that gains an override-aware number is added there, and only there.
- **B21** unified the duplicated cache keys: `["plexLibraries"]` → `["plex-libraries"]`
  (the spelling `PlexPanel` already invalidates) and `["vocab-values", …]` →
  `["vocabulary-values", …]`.
- **B22 + PR2** are both in `api.ts`, and both are now shared helpers rather than four copies:
  **`parseBody`** turns an unparseable success body into a plain-language `ApiError`, and
  **`throwIfFailed`** is the not-ok half of `request`, used by the four calls that cannot go
  through it (two blob downloads, the raw-body upload, and the paged queue that reads its totals
  off the headers). PR2 adds **`setUnauthorizedHandler`**, wired in `main.tsx` to
  `setQueryData(["me"], null)` so a dead session drops the app back to `Login`. Two details that
  are load-bearing: `/api/auth/*` is **exempt** (the gate's own probe 401s for every signed-out
  visitor), and the handler **writes** rather than invalidates -- answering a 401 with a refetch
  that 401s is a loop. A wrong password is a **403** server-side, not a 401, so it never signs
  anyone out.
- **PR3** gives both rule editors' vocabulary queries an `error` branch that replaces the whole
  add-a-rule form with a notice. **PR4** keeps "Turn off" rendered when the safety state can't be
  read (with its own error state and a confirmation, since the banner above still can't confirm);
  "Turn on" stays gone, because arming against a state we never read is arming on a guess.
- **B20** distinguishes container-missing from genuinely-empty on both `ServiceModal` pickers.
  **B19** gives `ServiceModal` `canClose={!save.isPending}`, a disabled Cancel, and the same
  `savePendingRef` Back-guard arrangement `ScheduleModal` already had. **B18** scopes
  `GeneralPanel`'s re-seed to the keys the mutation actually sent (`onSuccess(data, sent)`), so
  saving one row no longer discards another row's typing.

Verification: 22 new tests across a new `api.test.ts`, `useSafety.test.ts`,
`components/DeletionToggle.test.tsx`, `components/GeneralPanel.test.tsx`, plus additions to
`useOverrideMutations.test.ts`, `ServiceModal.test.tsx` and `PolicyEditor.test.tsx`. Every one
was reverted against and re-run; all fail without their fix. `GeneralPanel` is now exported
from `Settings.tsx` for its test, the way `SecurityPanel` already was. Two incidental fixes:
`PolicyEditor.test.tsx`'s simulate fixture was missing `unknown_size_items`, which crashed the
simulator as soon as a test rendered it; and a `mockRejectedValue` in a `beforeEach` is an
unhandled rejection before the query consumes it -- use `mockImplementation(() =>
Promise.reject(…))`.

Driven live in Chrome on the real app, everything restored afterward: **B18** -- typed a URL in
one row, saved the *name* row, and the URL kept both its value and its Save button (before the
fix both vanished); the name was set back and `/api/settings/general` re-checked. **PR3** and
**PR4** were driven by temporarily raising a 503 from `get_vocabulary` and `get_safety`
(reverted): both rule editors showed the notice with **zero** empty pickers left, and the
Deletion card showed the amber "couldn't confirm" line *and* a live "Turn off" beneath it.

**Two traps for the next live pass.** (1) React Query's `retry: 1` means an error branch lands
about a second *later* than you expect -- an early DOM check reads as "the fix didn't work".
(2) A long-lived tab can be running a stale HMR module; if a change refuses to appear, check
`performance.getEntriesByType("resource")` for the module's `?t=` URL and confirm it carries
your edit before debugging the code. Both cost time here.

### Batch 5, contrast and motion (U1, U2, U4, U5, U6)

Five findings that shared one shape: a color or a movement chosen for how it looked and never
measured. Every ratio below was computed against the tokens as committed, in both themes,
against every ground the text actually lands on.

- **U1 mints `--accent-text`**, and it is now the ONE accent-colored ink. Light darkens the
  accent 42%; dark keeps it (the sky already reads 8.47:1 on `--surface` there). 34 sites
  moved. The tightest ground is not `--surface` (2.03 → 5.50) but the docs index's own
  selected fill, a 22% accent tint, at 1.72 → **4.66**; that is the number the 42% was tuned
  to, so a smaller darken is a regression, not a taste call. **Borders, fills, `accent-color`
  and the `.brand-mark` logo keep the raw `--accent`** -- a logotype is the one graphic WCAG
  exempts, and the mark carries no words. Four sites beyond the review's 21 went too, on the
  same rule: `.select-toggle` / `.season-expander` / `.service-add` hover text, the docs
  sections' hover, `.filter-mi-tick`, the note callout's icon, `.gate-mark` (a "✓" at 1.81:1
  on its own tint), the two `text-decoration-color` underlines, and
  `.docs-index-item.active small`, whose accent/muted mix was 2.80:1 and now mixes from the
  ink instead.
- **A custom accent is the part a token alone cannot answer,** so `accent.ts` gained
  **`accentText(hex, theme)`**: it pushes the accent toward black (light) or white (dark) in
  2% steps until it clears 4.5:1 against that 22% tint, and `applyAccent` writes BOTH
  `--accent-text-light` and `--accent-text-dark` -- both, because "Match my device" can flip
  mid-session and the stylesheet picks by media query without asking the module again. The CSS
  reads them as `var(--accent-text-light, <the 42% mix>)`, so the built-in accent needs no JS
  at all. `index.html` pre-paints them by **reading back** a cached pair, never recomputing
  (the favicon's pattern). A test pins `accentText(DEFAULT_ACCENT, "light") === "#157194"`,
  which is exactly the stylesheet's fallback: if those two ever disagree, the page changes
  color the moment the module loads.
- **U2 moved 18 `--faint` strings to `--muted`.** The token's own comment already said "Never
  put text on --faint"; every surface built since the last review re-adopted it. The nine
  remaining uses are exactly the glyphs the comment names (two chevrons, two poster marks, the
  search glyph, an icon, the external-link arrow, the unticked setup disc, a disabled number),
  and the comment now says a new one carrying a word is the regression.
- **U4** put a real ring back on `.reap-confirm-input:focus`. It was the only `outline: none`
  in the file without a replacement, at 0-2-0 over the global `:focus-visible`'s 0-1-0, on the
  field that types the phrase releasing a removal. It now wears the control standard's own
  ring. Every other `outline: none` was audited: all six pair with a `:focus-within` ring on
  the composite parent or an explicit box-shadow.
- **U5** gave `.auth-aurora` a `prefers-reduced-motion` block. It was the only animation in the
  file over five seconds (22s, full-bleed, forever) and the only one without an opt-out, on the
  first screen and the one you cannot skip.
- **U6** put `--tap-min: 24px` beside `--ov-btn-w` as the one declaration of the hit-area
  standard, and `.bar-x` (which was hand-sized to 24px) now derives from it, along with
  `.fchip-x`, `.fchip-body`, `.filter-chip button` and `.tag-chip button`. Both `.fchip`
  children needed it, not just the x: the body EDITS the filter and the x CLEARS it, flush
  against each other, so an undersized pair means a thumb aimed at one lands on the other. The
  chips' side padding was trimmed to hold their overall size steady.

**One fix beyond the list, same rule.** The 375px pass found `.link-btn` ("Clear all", beside
the filter chips) at 23.5px tall with 5.6px of clearance from the chip's x -- rule 19, same
family, so it carries `--tap-min` too. Its other two uses are the why-panel's show-more and
back-to-show. After that the only sub-24px target left anywhere is an `<a>` inside a sentence,
which SC 2.5.8's inline exception covers explicitly.

Verification: 6 new tests in `accent.test.ts`, each reverted against. Two reverts worth
knowing: flattening the ground from the 22% tint to the plain surface fails BOTH the tightest-
ground test and the stylesheet-agreement test (the search stops at 36%, the CSS says 42%), and
making the search always darken fails the dark-theme case.

Driven live in Chrome on the real app, everything restored afterward. The color work was
verified by **audit, not by eye**: a walker over every element with its own text, flagging any
whose computed color is the raw accent or `--faint`, run on Review / Policy / Reap / Scales,
all eight Settings tabs and the docs modal, in **both themes** -- zero findings in each. Spot
measurements confirm the values (`.chip-tv`, the banner link, `.doc-kicker`, `.doc-table td.hi`
and the step numerals all at `rgb(21, 113, 148)`; `.jobrow-sched`, `.docs-index-h` and
`.docs-index-foot` at `rgb(102, 107, 120)`). A real filter chip measures 24x24 on the x and
88x24 on the body. **U4 was checked without going near the deletion path**: the finding was a
cascade bug, so a `.reap-confirm-input` built off-plan and focused reports
`outline: 2px solid rgb(37,195,255)`, offset 1px, red border intact. **U5 likewise** -- the
device does not ask for reduced motion, so the proof is that a live `.auth-aurora` really runs
`drift`, and the opt-out ships in the served sheet at the same specificity but later in source
order, so it wins.

**For the next live pass:** Chrome will not size a window below ~500 CSS px, so the 375px check
is a 375px `<iframe>` of the app injected into the page -- a true 371px viewport, and
`scrollWidth - clientWidth` proves no sideways scroll. Worth remembering for batch 6.

### Batch 6, copy (S2, S3, U3, U7, U8, U9, U10, U11, U12, U14, U15, U18, U19, I1)

Fourteen sentences an operator reads while deciding what to delete. Every one of them said
something that was not true of the code underneath it. Proof sheet (verbatim before/after,
plus the measurements behind the four that are not just strings):
four that are not just strings; it was a review artifact and is not kept with the repository.

- **S2 corrects the copy, not the fence** -- the backend's own comment says the allowlist is
  deliberately "scanning, planning, and editing the policy and reap profile", and `/api/profile`
  is the run caps, the grace countdown and the unknown-size allowance. The help now names what a
  key really can change (policy, run limits, grace) and what it cannot, and carries a comment
  saying that changing either list in `api/middleware.py` means changing this line in the same
  commit.
- **S3 is the one fix in the batch that moves the fence.** `/api/logs` and `/api/logs/download`
  joined `_API_KEY_READ_DENY`. They are not a secret store, but they are a running transcript of
  the library, and the download concatenates every rotating file, so one GET is the whole
  history. `PUT /api/logs/level` was already refused by the write allowlist. Two pytest
  assertions, both proven to fail without the change.
- **U3** builds `tvKeepClauses` beside `keepClauses`, each clause pushed only when its own
  switch is on. **Proved on real data:** this server's saved TV policy has the season floor at 0
  and "keep a show's first season" off, so the old line read "always keeps the newest 0 seasons
  of a show and anyone's mid-binge" -- both false -- on the sentence you scan before arming. It
  now reads "always keeps anyone's mid-binge". The gate clauses are deliberately NOT folded into
  the TV list: they read as conditions ("keeps anything watched by 3+ people") and these read as
  things ("keeps the newest 2 seasons"), and one list cannot carry both grammars in a line that
  has to be read at a glance.
- **U7 states a bound rather than a wrong number.** Under "Requested only" the keep-last floor
  reaches requested shows plus every show Reaper cannot tell about (`_keep_last_applies` keeps
  Unknown on purpose), and that set is not derivable from the frozen snapshot -- it needs the
  live request index. So the advisory says "up to N of M" under that scope, and the
  everything-is-protected warning styling stops there, because a bound cannot assert it. **A
  precise count remains open** and would need `season-shape` to consult the request index at
  render time; noted rather than silently skipped.
- **U8 compares the mix's SHAPE, not the mix.** `applyPreset` writes the shipped mix and then
  rescales the whole removal lane back to the 100-point budget, so with any custom rule the
  stored built-ins are the mix times a factor. Exact equality therefore read "Custom" on the very
  click that applied the preset. `rescaleToBudget` split into `rescaleTo(weights, target)`;
  `weightsMatchMix` is exact when there is nothing to rescale and allows the one point the
  largest-remainder arithmetic can itself move a weight when there is. **Driven live:** with one
  staged rule, clicking Balanced left the built-ins at 58/17/8 against a shipped 70/20/10 --
  which the old test could only call Custom -- and the segment now lights.
- **U9** adds the `window_days` control the server's own warning tells the operator to change
  (`GateMeta.window`, the dormancy row's picker, help bound directly beneath it). The warning
  already anchored to the gates block via `f.startsWith("gates.")`.
- **U18: a scroll listener, NOT an IntersectionObserver, and the reason matters.** An observer
  fires when a heading *crosses* the line; the last heading never can, because the document ends
  first -- so Deletion, the section that arms a removal and the one this finding named, stayed
  unmarkable. It was built with an observer first and the dead zone showed up in the live pass.
  Measuring from positions has no such gap, plus a bottom-of-page rule and a does-not-scroll
  rule. The line is read back off the stylesheet's own `scroll-margin-top` on
  `.policy-section` (rule 67), so a jump and the highlight cannot drift.
- **U15 went further than the finding, deliberately.** The review said the docs drifted from
  copy "the app already standardized"; the grep came out the other way -- six "dry run" against
  one "practice run". "Practice run" is the plainer of the two (rule 21), so the whole flow now
  says it: the button, both `ReapConfirm` outcomes, the plan blurb, the docs node. "Dry run"
  stays the API's and the executor's word internally. Same pass took "abort" out of operator
  copy in four more places (`cheatSheet`, `arming`, and both aborted-run reports), since the
  app-wide reap bar already reported that state as "Stopped." **This is the one change in the
  batch worth a veto if you disagree** -- it is mechanical to reverse.
- **I1** says what the practice run proved. The old lead was `0 souls were actually reaped`,
  zero by construction, and "steps" was wrong twice: the executor records exactly one
  `StepOutcome` per item, so a 3-season plan reported "3 steps" over 9 journalled steps in the
  table below. The false comment is gone and the rows key on `media_key` alone.
- **U10 / U11 / U12 / U14 / U19** are single strings: "this reap" not "a scan"; the container's
  console output, not "its log" (and `mint_recovery_token`'s docstring, which was the source of
  the wrong copy); "incomplete" not "degraded"; plain language instead of `Request failed (502).`
  with the status moved to a console warning; "per day" not "per 1 days".

**Four fixes beyond the list, all the same rule (21) or the same mechanism.** `arming.ts` and
both aborted-run reports lost "abort" alongside U15's two; `mint_recovery_token`'s docstring
went with U11.

Verification: 17 new tests. Every behavioral fix was reverted one at a time and its test watched
to fail -- U3 (3), U7 (1), U8 (1), U9 (1), U14 (2), U19 (1), I1 (2), S3 (2). **Driven live in
Chrome on the real app, everything restored afterward**: the TV summary against this server's
real policy, the advisory flipping to "up to" when the scope narrows, the preset segment lighting
with a staged custom rule, the rail tracking all four sections including Deletion at the very
bottom, the API-key help, and "✓ Practice run passed: the plan is sound, and it sent nothing." on
a real 3-item plan with deletion off. The policy draft was edited and **Discarded**, and the
saved policy re-read afterward to confirm it was untouched. **No reap was executed and deletion
was never armed.**

**Two things the next session should know about this environment.** The background Chrome tab is
`document.visibilityState === "hidden"`, so the browser never runs the rendering steps that
deliver IntersectionObserver callbacks, `requestAnimationFrame`, or native `scroll` events --
`window.scrollTo` moves `scrollY` silently. Anything driven by those has to be flushed by hand:
dispatch `new Event('scroll')`, and capture the rAF callback with a temporary shim that *queues*
rather than *calls* (calling inside the shim recursed and froze the renderer once). And S3's live
proof was deliberately skipped: confirming a 403 needs the operator's real API key, and reading a
stored secret into the transcript is not worth it when a pytest assertion covers it exactly.

See the batch 7 section below for what actually happened to those.

### Batch 7, refactor and performance (R1-R9, P1-P9, H1-H3, B30-B35, I2)

The last batch. 26 of its 28 findings are done; R1 and R2 are annotated in place with the
residue. One backend change (H1), one schema change (P3), and the rest frontend.

**One contract crossed the wire.** H1: a chip now carries its own `why` clause
(`ChipOut.why`, produced beside `text` in `_chip`). The frontend used to recover it by slicing
`"Kept · "` off the chip text and looking the rest up in a hand-copy of the backend's prose --
so rewording one chip server-side would silently drop every held-reap explanation to a generic
fallback, with the tests green because both sides asserted the same transcription. `chipWhy` is
now one line. I2 falls out of it: the frontend fixtures give `text` and `why` deliberately
different words, so a test that went back to parsing the text fails.

**The measurements, on real data, in the browser.**

- **P3** was the big one. `GET /api/runs` built a full `RunOut` per row, and each one re-read
  the whitelist, the profile and the entire condemned candidate set of that run's snapshot.
  Timed against 12 stored runs: **570 ms -> 6 ms**. The list is now `RunSummaryOut`, seven
  stored fields, and opening a row fetches that one plan. It was dishonest as well as slow: a
  finished run's confirmation phrase was recomputed against TODAY's overrides, describing a
  plan nobody ever approved, so the history row drops it and states the run's state in plain
  words instead (`runState()` in `ReapPlan.tsx`: "not run" / "running" / "done" / "stopped").
- **P4**: the app shipped as one 551 kB script (159.55 kB gzipped). Now **317 kB / 94.8 kB
  gzipped** at first paint, with Policy, Reap, Scales, Settings, the setup wizard and the docs
  behind `React.lazy`. Confirmed live: no route module is fetched at first paint, and clicking
  Policy fetches exactly its own chunk and nothing else.
- **P1/P7/P8** are the queue's render loop. Cards are `React.memo`'d, `toGroups` and every
  derived array are memoized, the handlers are stable, and `CardSelect` no longer carries the
  per-card `isSelected` (which made the object per-card and defeated memoization outright).
  `useDefaultSpareDays` / `useHoldsBackUnmeasured` now read one shared subscription
  (`queueSettings.tsx`) instead of opening a query observer per control -- roughly a thousand
  of them on two keys with 400 cards and their seasons expanded.

**A real bug the perf work exposed, not caused.** With the queue no longer re-rendering
freely, `useReviewFreshness`'s "a silent refresh failed, so nudge instead of leaving the list
silently stale" (PR-5) stopped firing: it detected "a refetch happened" by watching the list's
`isFetching` flag go true and then false, which only worked if a render happened to be
committed during the fetch. A refetch that rejects in the same microtask flush never renders as
fetching at all. It now waits on what the refresh RETURNS -- `refreshReview` hands back the
invalidation promise -- so the signal does not depend on the render schedule. Three tests cover
it, including the synchronous-rejection case that could not pass before.

**What was driven live** (real data, deletion never armed, nothing reaped): B32's backwards
ramp refuses "from 5 years to 1 year" with the Add button disabled and the reason beside the
boxes, instead of silently rewriting the bound to 1826 days; B31's Discard clears the
switch-media warning that used to survive it; B30's tab switch issues **zero** requests pairing
the new lane with the old tab's filters (it used to fire one, draw it, then fire the right one);
P3's history row opens a plan on demand with its phrase and step table intact; every route
renders with no error notice and a clean console. The seeded filter used for B30 was removed
afterward and the operator's remembered filters left as found.

**H2 is the one fix verified by test rather than by screen.** The j/k queue loop only runs on
the Review view, and no modal is reachable from there in the current UI, so there is nothing to
open over it. The three tests in `backnav.test.tsx` include the discriminating case the old DOM
probe could not answer: an overlay that carries `role="dialog"` without being modal must NOT
take the keyboard.

**Judgment calls worth a second opinion.**

- **R7 moved the docs modal's breakpoint from 720px to 640px** rather than keeping 720 and
  writing a justification. The file documents a 1100/900/640/560 grid and 720 was the only stop
  off it -- the same one-off the previous review removed. Below 900 the modal is already a
  full-screen sheet, so at 640 the split still has the whole viewport (236px index + ~400px of
  prose). If that reads too narrow on a real 650px window, move it back and write the comment.
- **P5 does not use the review's suggested key list verbatim.** It names ten caches with a
  reason each, and deliberately drops `["runs"]` (after P3 that response is stored rows a scan
  cannot change) in favor of `["run"]`, whose derived numbers a scan does change.
- **`.btn-plex` in R4's dead-CSS list is alive** (Login and PlexPanel both use it). What was
  dead is the `.plex-status` block including its `.plex-status .btn-plex` descendant rule; that
  went, and the `.btn-plex` rule at the top of the file stayed.

**Shape changes the next reader will meet.** `ReviewQueue.tsx` now imports from five new
siblings -- `reviewFate.ts`, `OverrideControls.tsx`, `queueIcons.tsx`, `queueFilters.tsx`,
`queueSettings.tsx` -- and re-exports the old names for one commit so test imports do not churn;
`PolicyEditor.tsx` likewise re-exports `andList` / `weightsMatchMix` from `policyPresets.ts`.
`api.runs()` returns `RunSummary[]`, not `Run[]`. Every fetch in `api.ts` goes through
`fetchApi` (R3), so a retry or timeout added there covers the whole surface. `useModalOpen()` /
`useModalLayer()` live in `backnav.tsx`. `index.html` computes no color: it reads
`reaper-accent-ink` back from the cache `applyAccent` writes (B34), and `accent.test.ts` fails
if any luminance constant reappears in that file.

### Batch 8, the four that mislead or dead-end (B6, B10, B11, B17)

Off the fix order, which ended at batch 7. These are the unscheduled findings that either lie to
the operator, strand them, or write somewhere nobody asked for. Each was reverted against and
re-run: all six tests fail without their fix.

- **B6** was a dead end with no exit. A profile that could not be read shows the shipped caps and
  a notice saying a scan will remove nothing "until you check these and save" -- and `paceDirty`
  was a plain JSON comparison, so nothing was dirty, the savebar never rendered, and there was no
  Save to press. The only escape was to change some value on purpose; restoring the intended caps
  and saving them was impossible. `settings_recovered` now forces dirty exactly as the policy
  half's `fell_back` does, and the comment on each names the other.
- **B10 was two bugs with one shape,** and the backend one was the dangerous half. In the panel,
  `currentServer` fell back to `servers[0]`, so a partial plex.tv response promoted a *different*
  server to "the one Reaper manages". Dropping the fallback was not enough on its own: a `<select>`
  whose value matches no option displays its FIRST option, so the other server still read as
  current, merely unsavable. The box now carries one option naming the linked server, both pickers
  go quiet, and a notice says the list came back without it. **Server-side, `PUT
  /api/settings/plex/connection` probed a typed address for a pulse and nothing else,** so an
  address belonging to any other Plex on the network saved successfully -- and Reaper then wrote
  Leaving Soon collections and labels into, and read Never-Reap from, a library nobody pointed it
  at. It now asks who answered and refuses a mismatch with a 409. The revert proves it: without
  the check the wrong server's address lands in `connection_uri`.
- **Rule 24, same finding.** `probe_connection`'s docstring claimed `/identity` "doubles as a
  check that we reached the server we think we did" and it never compared anything. Rather than
  only correcting the prose, the check now exists as `connection_identity`, and `probe_connection`
  keeps reachability-only semantics **on purpose**, with the reason written down: its caller is
  the link flow walking addresses plex.tv just advertised for one resource, and requiring an
  identity there would make a Plex that does not report one impossible to link at all.
- **B11** let a poll that resolved after the sign-in settled call the handlers anyway, because
  `stop()` cleared the timer and nothing else. Every poll now carries its run and a finished run
  ignores late answers, plus an in-flight guard so ticks stop stacking requests. The two tests
  cover both directions: a rejection after the run ended must not paint "sign-in failed", and a
  success after Cancel must not sign anyone in.
- **B17** closed the modal on any click whose release landed on the scrim, and a drag out of the
  panel dispatches `click` on the scrim by definition (a click fires at the common ancestor of
  press and release). The scrim now closes only when the press both began and ended on it.

**Driven live in Chrome, deletion never armed.** B17 is the one this batch could prove on a
screen: text typed into a service modal, then dragged from a field out onto the scrim -- the
modal stays, the typed value survives, and the drag really does select text; a genuine outside
click still closes it; and the API confirms nothing was written. The Plex tab was re-checked for a
B10 regression in the ordinary case: no notice, both pickers live, the server select holding a
real value. B6 needs an unreadable stored profile, B10 needs a filtered plex.tv response, and B11
needs a slow plex.tv, so all three are covered by test rather than by unlinking the operator's
real server.

**One process note.** The gate command used through batches 1-7 piped each step into `tail`,
which returns `tail`'s exit status, not the step's. `tsc` failed in this batch and the chain
still reported success. Run the gates with `set -o pipefail`, or unpiped.

### Batch 9, the wrong numbers and the wrong claims (B23, B26-B29, PR7)

Also off the fix order. Where batch 8 was surfaces that dead-end, these are surfaces that state
something untrue: two numbers for one person, a warning tracking a value nobody is looking at, a
sentence asserting caps whose settings could not be read, a tile hidden exactly when it is needed,
a save button held by an unrelated half, and a tab click that throws away typed input. Six of the
seven fixes carry a test proven to fail without them (reverted one at a time, re-run, restored);
B23 has no unit test and was driven live instead.

- **B28** had the Scales card and the panel it opens divide by two different sets. The roll-up
  counted every matched request including a season-scoped one that scoped to nothing, which the
  detail builder explicitly skips, so the same person in the same scan read one watched share on
  the card and a different one in the panel. The roll-up now skips it too, in the same shape and
  with the same reason written down. A person whose ONLY requests scope to nothing now gets no
  card at all, which is the other half of the same agreement: the drawer has no detail to show
  them, so it 404s, and a card that opens onto nothing must not exist.
- **B27** nested the "Not in the last scan" tile inside the has-people branch, so the one
  affordance that explains an empty Scales page was hidden in exactly the state that produces it
  (a fresh portal, or ids the scan has not backfilled, leaves every request unmatched). The tile
  is defined once now and rendered in both states.
- **B26** anchored the unknown-size warning beneath the box that sets it, then computed it from
  the SAVED profile. Every other warning in that editor describes the draft, so this one was the
  odd one out: raise the allowance and nothing appeared until after a save, lower it and the old
  warning kept naming the old number. The editor sends the drafted value with the check
  (`PolicyValidateIn.draft_max_unmeasured_per_run`, debounced on the same timer as the policy),
  so `inspect` stays the single author of the message. Omitting the field keeps the stored
  reading, which is what every other caller wants, and the bound is on the wire so a draft can
  never describe an allowance a save would refuse.
- **B29** let the pace clause fall back to "removes only within your caps" when the profile query
  had *failed*, so the sentence an operator scans before arming asserted caps were in force
  directly above a section saying those settings could not be loaded. The clause is dropped
  entirely on a failed read; the neutral wording now covers the still-loading case alone.
- **PR7** gated two deliberately independent saves behind one condition, so a policy off the
  100-point budget also blocked the pace save that has nothing to do with it. Still one save
  affordance (rule 43), now gated per half: the button enables when either half is savable, the
  "what applies when" line describes what will ACTUALLY be written rather than what is merely
  dirty, and a line names the half being left behind. That line renders only when the other half
  IS being written -- with the policy alone dirty the button is simply disabled and the notice
  beside the cause already says why, so a line there would be the bar's third sentence on one
  subject.
- **B23** cleared `settingsFocus` on every masthead tab click, including a click on the tab you
  are already on, and `<Settings>` is keyed on that nonce -- so the click remounted the whole
  subtree and destroyed unsaved input. The focus resets are skipped when the clicked tab is the
  current view.

**Driven live in Chrome, deletion never armed, nothing written** (confirmed against
`/api/settings/general` and `/api/profile` afterward: the name, grace, and allowance are all
unchanged). B23 both ways: arriving at Settings through a "Settings → Plex" jump, typing into a
field and clicking the Settings tab leaves the typed value intact, while a genuine tab change
still clears the focus and lands on General with no jump replayed. B26 both ways: the warning
appears beneath the box the moment the box reads 5 and clears the moment it reads 0. PR7: with the
removal lane off budget and grace edited, Save is live, the bar says "Save writes pace and limits
only", and the applies-when line is the pace one. B28's agreement was read off a real person's
card and panel (both figures matched); the divergent case is the unit test's.

---

### Batch 10, the hygiene four (S4, S5, U16, PR11)

The last of the unscheduled findings, and the smallest: nothing here is visible to an operator on a
good day. Two are about a secret or a handle living longer than the thing it was for, one is a unit
mismatch that has now been found three times, and one is a build setting with no note saying whether
it was a decision. All four carry a test proven to fail without them.

- **S4** opened the plex.tv auth popup without `noopener`, so plex.tv held a `window.opener` handle
  on the window Reaper's sign-in page runs in and could navigate it -- to a look-alike of the page
  that takes the operator's Reaper password. `PlexPanel.startLink` already passed `noopener`; this
  one kept the handle to close the popup itself. Those cannot both be had: a browser permits
  `close()` on a cross-origin window *because* you are still its opener, so nulling `opener`
  (the finding's second option) would have dropped the close too, while looking like it kept it.
  `noopener` it is, and the ref and its five `?.close()` calls go with it. The cost is real and
  named in the code: the Plex window now stays up until the operator closes it, which is already
  what the Settings link flow does.
- **S5** left the admin password in component state after the form holding it closed -- in
  `DeletionToggle` on Cancel, where it refilled the box the next time deletion was armed, and in
  `RestoreCard` on a failed confirm. The fix follows one rule: clear it on every path that unmounts
  the field, because that is where it stops being visible and starts being retained. That turned up
  a third site the finding did not name -- `RestoreCard.choose()`, where staging a second backup
  dropped the summary and brought the box back holding a password typed against a different file.
- **U16** sized `.docs-index` in `vh` inside a modal this file sizes in `dvh`, in a block that only
  applies on the narrow screens where the two units differ. Both named sites are fixed, plus two the
  finding missed (`.why`, `.why-loading`). Since no bare `vh` is left, the convention is now stated
  once at the top of `index.css` and enforced by `viewport-units.test.ts` -- the same mismatch had
  already been found and commented twice, and a third comment would not have stopped a fourth.
- **PR11** shipped source maps with nothing saying whether that was a decision. It is, so the note
  is what changed. Two of the reasons to drop them do not survive checking: a browser fetches a
  `.map` only with devtools open, so first-load transfer is unaffected, and the sources are AGPL and
  published with no build-time value in the bundle. That leaves ~2 MB in the image against an
  operator's console being the only debugger available on a server we will never see.

Not driven in a browser, deliberately: S4's popup and S5's arming form both terminate in a password
prompt (plex.tv's, then Reaper's own), which is not mine to type, and U16/PR11 are a CSS unit and a
build flag with no interactive surface. What the tests pin is exactly the changed behavior -- the
features string passed to `window.open`, the value left in each password box, and the absence of a
bare `vh` in the stylesheet.

---

## 1. Security

- [x] **S1 [critical]** `frontend/src/components/ReapConfirm.tsx:67` · The execute mutation posts
  `run.confirmation_phrase` (the phrase the server already stored) instead of `typed`, so the typed
  confirmation never leaves the browser. The server recomputes the expected phrase live
  (`api/runs.py:386-396`) and compares it to what was posted, so the content binding still holds,
  but the *human* gate is reduced to a client-side `disabled` attribute: the server cannot tell an
  operator who typed the phrase from a client that echoed it. It also deadlocks. When the expected
  phrase moves after the sheet opens (a spare or reap from another tab, or raising the unknown-size
  allowance, which `_planned_candidates` reads live), `phraseOk` at :102 only lights the button for
  the *stale* phrase, the mutation sends the stale phrase, the server 409s and prints the real one,
  and typing that real phrase disables the button. The plan becomes unexecutable and un-typeable.
  **Fix:** send the operator's text — `api.executeRun(run.id, typed.trim())` — and on a 409 refetch
  the run so the sheet re-seeds `run.confirmation_phrase` and the typed check measures against the
  phrase the server will actually accept.

- [x] **S2 [high]** `frontend/src/components/Settings.tsx:499-504` · The API-key help says a key
  "cannot change any setting", but `_API_KEY_WRITE_ALLOW` (`src/reaper/api/middleware.py:85-94`)
  includes `/api/profile` — the run caps, the grace countdown, and the unknown-size allowance. A key
  holder can set `caps_enabled: false` and `max_unmeasured_per_run: 25`, then post a permissive
  `/api/policy`, and the operator's next hand-approved reap runs far past the pace they configured,
  with nothing in the browser having changed. The operator decides whether to hand this key to a
  third-party dashboard on the strength of that sentence. **Fix:** the backend's own comment says
  the allowlist is deliberately "scanning, planning, and editing the policy and reap profile", so
  correct the copy rather than the fence: name exactly what a key can change, in `GeneralPanel`. If
  the profile write is *not* meant to be reachable, drop it from `_API_KEY_WRITE_ALLOW` instead, but
  do one or the other in the same change.

- [x] **S3 [medium]** `frontend/src/components/Settings.tsx:500-503` · The same paragraph
  understates the read scope. `_API_KEY_READ_DENY` holds only two paths, so `GET /api/logs` and
  `/api/logs/download` are reachable with the header alone. A leaked automation key downloads the
  full rotating logs, which carry titles, usernames, and root-folder paths, while the operator
  believes the key can only "read your library". The backup download was deliberately denied for
  exactly this reason; the log was not. **Fix:** add `/api/logs` and `/api/logs/download` to
  `_API_KEY_READ_DENY`, and keep the `GeneralPanel` copy in step with S2.

- [x] **S4 [low]** `frontend/src/components/Login.tsx:70` · The Plex login popup is opened without
  `noopener`, so plex.tv holds a `window.opener` handle to Reaper's origin window and can navigate
  it to a look-alike login. `PlexPanel.startLink` (`PlexPanel.tsx:126`) already passes `noopener`.
  **Fix:** match `PlexPanel` — open with `noopener` and drop the `popup.current?.close()` the handle
  exists for, or null the `opener` immediately after `window.open`.
  **Done (batch 10), taking the first option.** The two are not interchangeable: nulling `opener`
  disowns the popup, and a browser lets you `close()` a cross-origin window *because* it is still
  your opener, so that route drops the close as well while looking like it kept it. The handle,
  the ref and all five `?.close()` calls are gone; the Plex window now stays up until the operator
  closes it, exactly as the Settings link flow already left it.

- [x] **S5 [low]** `frontend/src/components/DeletionToggle.tsx:100-102` · Cancel clears `confirming`
  but not `password`, so the arming password stays in component state for the life of the panel and
  repopulates the field when the form reopens. `RestoreCard` (`Settings.tsx:937-940`) has the same
  gap on a failed confirm. **Fix:** `setPassword("")` in both cancel/catch handlers, matching what
  `onSuccess`/`reset()` already do.
  **Done (batch 10).** The rule the fix follows: clear the password on every path that unmounts
  the field, since that is where it becomes invisible-but-retained. In `RestoreCard` that is
  `choose()` as well as the failed confirm the finding names -- staging a second backup drops the
  summary, and the box came back holding a password typed against a different file.

## 2. Bugs

- [x] **B1 [high]** `frontend/src/components/ReviewQueue.tsx:2810` · `MovieCard` receives
  `hideReap={verdict === "condemn"}` — the *tab's* verdict. Rule 48 names this exact substitution as
  a regression and `reapIsNoop` (:1019) is the one sanctioned test, used correctly at
  `SeasonList:1605` and `WhyPanel:1000`. Lane membership is the *effective* verdict server-side
  (`services/condemned.effective_verdict:102-117`), so a movie whose stored verdict is
  `abstain`/`protect` with an honored hand reap sits on Condemned with `verdict !== "condemn"`:
  `reapIsNoop` is false, Reap must stay, but the tab test hides it, leaving a resting scythe
  `OverrideMark` with no control to clear it. The mirror case: `reapIsNoop` deliberately returns
  false for a *spared* condemned item so it can be flipped back, and the tab test hides that too.
  **Fix:** pass `hideReap={reapIsNoop(group.items[0]!)}` in the `shownGroups.map`; the prop itself is
  fine, only the call site is wrong.

- [x] **B2 [high]** `frontend/src/components/ReviewQueue.tsx:1348-1358` · The `season-expander`
  button stops propagation on click but has no `onKeyDown` guard, while it sits inside a `.card-head`
  that owns Enter/Space and calls `e.preventDefault()` (:1805-1810). Pressing Enter on a focused
  "5 seasons" pill cancels the button's own activation and opens the show panel instead: keyboard
  users cannot expand a show at all. Its sibling `SeasonStrip` square (:1315-1319) carries the guard,
  with a comment explaining precisely this. Rule 60. **Fix:** copy the `SeasonStrip` guard verbatim
  onto the expander button and add a case to the B-7 describe block in `ReviewQueue.test.tsx`.

- [x] **B3 [high]** `frontend/src/components/ReapConfirm.tsx:64,130-209` · The sheet has no rendering
  for `status.phase === "error"`. When the executor raises mid-run (deletion switched off while
  running, a cap breach, a failed canary, a crash after N files were already deleted), `running` goes
  false and `report` stays null, so the Stage-2 block re-renders with the phrase still typed and the
  Reap button live again, saying nothing about what happened. Clicking it starts another task that
  refuses again, still silently. Reopening later via the bar's View fires a dry run against a
  non-PLANNED run, so the operator's only feedback about a reap that may have deleted files is "The
  dry run failed, so nothing can be executed." **Fix:** derive
  `failed = mine && !status.running && status.phase === "error"`, gate Stage 2 on `!failed`, render
  `status.error` in a `.notice.notice-error` with a Done button, and extend the auto-dry-run skip
  test at :96 to `s.report != null || s.phase === "error"`.

- [x] **B4 [high]** `frontend/src/components/ReapConfirm.tsx:80-88` · Every cache refresh after a
  real reap lives in this sheet's `endedRef` effect, but the sheet is explicitly designed to be
  closed mid-run (:16-20) and `ReapBar` (`App.tsx:110-188`) has no completion effect at all. Close
  the sheet during a reap and the review queue keeps listing deleted titles and the Reap ledger keeps
  promising to remove them until a manual reload. Even with the sheet open, `["reap-breakdown"]` and
  `["snapshot"]` are never invalidated, so the ledger directly above the plan is wrong the moment the
  run ends. **Fix:** move the completion invalidation into the always-mounted `ReapBar`, fired once
  on the running-to-ended edge guarded by a `useRef` on `run_id`, covering `["runs"] ["candidates"]
  ["reap-breakdown"] ["snapshot"] ["fairness"]`; keep `onDone` for the parent's own reaction.

- [x] **B5 [high]** `frontend/src/components/ReapBreakdown.tsx:120,160-163` · `reapCount` (:105)
  correctly subtracts the unmeasured hold-back for the headline and the "Will be reaped" total, but
  the movie/season split at :161 prints raw `data.movies` / `data.seasons` and the ledger rows above
  resolve to `will_reap`, so the same page states two different totals. With 569 effective condemned
  of which 4 are unmeasured, the headline reads 565 while the split reads "402 movies · 167 TV
  seasons" = 569 and the arithmetic 543 − 12 + 38 = 569. Worse, the empty-state gate at :120 tests
  `data.will_reap`, so when *every* condemned item is unmeasured the page renders a full ledger
  totaling 0. Rules 62 and 30. **Fix:** return the split over the plannable set (add
  `movies_unknown`/`seasons_unknown` in `services.breakdown.reap_breakdown`) and subtract them under
  `holdsBackUnmeasured` the way `reapCount` does; switch the empty-state test to `reapCount`.

- [x] **B6 [high]** `frontend/src/components/PolicyEditor.tsx:1462-1465` · A profile that fell back
  to shipped defaults renders the recovery notice at :2238 telling the operator "a scan won't remove
  anything until you check these and save", but `paceDirty` has no equivalent of the policy path's
  forced-dirty on `fell_back`, so `dirty || paceDirty` is false, the savebar never appears, and there
  is no Save to press. The only escape is to leave some value deliberately changed; restoring the
  intended values and saving is impossible. Compare :1570-1578, whose comment says it exists for
  exactly this reason ("Missing the second one left the only way out of the fallback behind a gate
  that never opened"). **Fix:** add `Boolean(savedPace?.settings_recovered) ||` to the `paceDirty`
  memo, and assert the savebar is present in the recovery test in `PolicyEditor.test.tsx`.

- [x] **B7 [high]** `frontend/src/components/QuantityInput.tsx:123` (same shape at :69) ·
  `FixedQuantity`'s `onChange(Number(e.target.value) || 0)` makes an empty box impossible: clearing
  the field to retype emits 0, the call site re-floors it (`v || 1`), React rewrites the controlled
  input to "1" with the caret at the end, and the newly typed digits *append*. Select-all, Backspace,
  "25" on "Most titles per run" saves **125**, which clears the server's `le=1000` and stores clean.
  Same path on `max_items_per_30d` and the people threshold; `Settings.tsx:468` imports the same
  component. **Fix:** give `FixedQuantity` a local string buffer — hold the raw `e.target.value`,
  emit `onChange` only on a successful parse, emit nothing for `""`, commit the floor on blur — then
  delete the `v || 1` / `v || 0` re-floors that exist only to paper over this.

- [x] **B8 [high]** `frontend/src/components/PolicyEditor.tsx:365-374` · The IMDb/TMDb rating bar
  passes `value={(rule.floor / 10).toFixed(1)}`, re-deriving the displayed text from stored tenths on
  every render, so the box is rewritten under the caret mid-typing and a decimal can never be
  entered. Type "7" over 6.5 and React rewrites it to "7.0"; the next "." makes "7.0.", which
  `<input type="number">` reports as `""`, which B7's coercion turns into **floor 0.0** — the "keep
  well-rated titles" bar now clears every title. Typing "5" instead gives 7.05, rounded to 7.1. Only
  the spinner arrows can reach 7.5. **Fix:** B7's string buffer, plus stop re-deriving the text from
  tenths on every render: keep the typed text local and convert on commit.

- [x] **B9 [high]** `frontend/src/App.tsx:52` · The permanently-mounted `SafetyBanner`'s `["safety"]`
  query has no `refetchInterval`, and the global default sets `refetchOnWindowFocus: false` with a
  30s `staleTime` (`main.tsx:8-18`). `destructive_enabled` is DB-backed with no auto-disarm, so a
  desktop tab sitting on Review keeps saying "Read-only. Reaper can look but can't remove anything."
  indefinitely after deletion is armed from a phone or a second tab — the fail-open direction on the
  app's one always-visible safety surface. The `scanStatus` query 550 lines below idle-polls at 15s
  with a comment saying exactly why. **Fix:** give `["safety"]` the same `refetchInterval: 15000` and
  `refetchOnWindowFocus: true`.

- [x] **B10 [high]** `frontend/src/components/PlexPanel.tsx:205-206` · `currentServer` falls back to
  `resources.data.servers[0]` when no server carries `current: true`, so a partial or filtered
  plex.tv response silently presents a *different* server as the one Reaper manages, and the
  Connection row lists that server's addresses. Picking one saves it via `plexSetConnection`, and
  Reaper then writes Leaving Soon collections and labels into, and reads the Never-Reap collection
  from, a server it was never linked to. **Fix:** drop the `?? servers[0]` fallback; when
  `find(s => s.current)` is undefined, render an explicit notice naming `data.name` and disable the
  Server and Connection selects. Separately, `probe_connection` (`clients/plextv.py`) does not
  compare the `/identity` machineIdentifier its docstring says it checks — fix or correct that
  comment in the same change (rule 24).

- [x] **B11 [high]** `frontend/src/components/PlexPin.tsx:86-105` · The PIN poll fires a new async
  request every 2s with no in-flight guard and no generation token, and `stop()` only clears the
  timer, so a poll that resolves *after* a final outcome still calls the handlers. When plex.tv is
  slow the polls overlap: poll B returns "ok" and signs the operator in, then poll A rejects (PIN
  consumed, or 429) and calls `onFailed`, painting a red "Plex sign-in failed" over a session that
  succeeded. Via `cancel()` the mirror case signs the operator in after they pressed Cancel.
  **Fix:** add a `runIdRef` bumped by `begin`/`stop`/`cancel` plus an `inFlightRef` skip, and ignore
  any result whose captured run id is stale before touching `setServers` or a handler.

- [x] **B12 [high]** `frontend/src/components/ReviewQueue.tsx:1774,1853,1862` · After a whole-show
  Spare the show card keeps asserting removal. `isReapTab = isCondemned(first)` reads
  `first.override`, which `patchShowOverride` deliberately does not touch (it patches only
  `show_override`), so the card tints `card-spared` and its chip says "will be kept" while the meta
  line one row below still reads "3 of 5 would be removed · 22.5 GiB" and the status line still leads
  with the amber dormancy pill. It persists all session because `settle(true)` invalidates with
  `refetchType: "none"`. Rule 61. **Fix:** in `ShowCard`, derive it from the show's own decision
  first — `showOverride === "spare" ? false : showOverride === "reap" ? true : isCondemned(first)`
  (`showOverride` is already read at :1786) — and keep `CardStatusLine condemned=` on the same value
  so the pill and the clause move together.

- [x] **B13 [high]** `frontend/src/index.css:580-592` · `.tab:hover:not(:disabled)` and
  `.seg:hover:not(:disabled)` are specificity 0-3-0 (the `:not()` adds a class's weight) and repaint
  `background: transparent` over `.tab.active` / `.seg.active` (0-2-0), with no `.active:hover`
  re-assertion anywhere. Hovering the masthead section you are on, the review lane you are on, or any
  shared `Segmented` option makes the raised pill lose its fill; in dark mode the pill (#191b21)
  becomes the track color (#22242c) and disappears, so pointing at your current tab reads as a
  deselect. Rule 47. The file already diagnoses and fixes this exact hazard for `.ov-btn` at
  :5111-5123, comment and all. **Fix:** add
  `.tab.active:hover:not(:disabled), .seg.active:hover:not(:disabled)` re-asserting
  `background: var(--surface); box-shadow: var(--shadow-sm);`, copying the `.ov-spare.active:hover`
  pattern verbatim.

- [x] **B14 [medium]** `frontend/src/useOverrideMutations.ts:99-107,112-117` · `settle()` and
  `refresh()` invalidate `["candidates"] ["group"] ["candidate"] ["reap-breakdown"]` but not
  `["snapshot"]` or `["fairness"]`, both of which the server computes override-aware (`_snapshot_out`
  shifts lanes by `overridden_lane_shifts`; `services/fairness.py:799` filters by the same effective
  set). Spare a large condemned title, then open Jobs within the 30s staleTime: the scan row still
  counts it as reclaimable, and so does that person's Scales figure. `ReviewQueue`'s own
  `refreshReview` (:2168-2176) does invalidate `["snapshot"]`, which is what makes this read as an
  oversight. **Fix:** add both keys to `settle()` and `refresh()`.

- [x] **B15 [medium]** `frontend/src/components/ReapPlan.tsx:109,214-223,247-253` · `run` is local
  state that is never re-seeded, so after a reap completes the page still shows the spent plan as
  executable: `onDone` invalidates `["runs"]` and the history row below re-renders as "completed"
  while the captured object still says `state === "planned"` and the red Execute button stays live.
  Clicking it can fire a dry run against a COMPLETED run, whose only explanation to the operator is
  "The dry run failed, so nothing can be executed." **Fix:** keep `runId` in state and read the run
  through `useQuery(["run", runId])` so the post-execution invalidation refreshes state, count, and
  phrase together.

- [x] **B16 [medium]** `frontend/src/components/ReapBreakdown.tsx:77,105-107,178-189` ·
  `useHoldsBackUnmeasured()` collapses "profile unknown" to `true`. That is the safe answer on a
  review card and the *unsafe* one here: when `/api/profile` fails while the allowance is above 0,
  the page silently shrinks the delete count and prints "N titles can't be measured, so Reaper won't
  remove them", the opposite of what the planner will do. The component handles its own query's
  `isError` at :82 but takes this one on faith. **Fix:** give the hook a tri-state
  (`{holdsBack, isPending, isError}`), keep `true` as the card default, and render an explicit
  "Reaper couldn't check your unknown-size allowance" notice here instead of an adjusted count.

- [x] **B17 [medium]** `frontend/src/components/ModalShell.tsx:115` · The scrim closes on any click
  whose mouseup lands on it. A text drag that starts inside the panel and ends outside dispatches
  `click` on the nearest common ancestor — the scrim — so the panel's `stopPropagation` never sees it
  and the modal tears down. Dragging across the mono confirmation phrase to read or copy it destroys
  the dry-run result and the typed phrase; the same gesture in `ServiceModal` destroys typed
  credentials. **Fix:** record the mousedown target on the scrim and close only when both the
  mousedown and the click originated on the scrim itself.

- [x] **B18 [medium]** `frontend/src/components/Settings.tsx:164-176` · `save.onSuccess` re-seeds
  name, url, timezone, proxies, and accent from the response, but each row has its own Save button,
  so saving one field silently discards every other in-progress edit: type a new application URL,
  then press Save on the name row, and the typed URL and its Save button vanish with no notice.
  **Fix:** in `GeneralPanel`, pass the submitted body through to the callback and re-seed only the
  keys the mutation actually sent.

- [x] **B19 [medium]** `frontend/src/components/ServiceModal.tsx:384-393` · `ServiceModal` passes no
  `canClose` to `ModalShell`, so the scrim, Escape, and ✕ tear it down mid-save and the failure is
  never rendered. Click the scrim while a PUT is in flight and the 409 "a service with that name
  already exists" lands on an unmounted component, `invalidate()` never runs, and the operator
  believes the change saved. `ScheduleModal` guards this at `Settings.tsx:1322`. **Fix:** pass
  `canClose={!save.isPending}`, disable Cancel while pending, and give `ServicesPanel`'s
  `useBackGuard` the same predicate the schedule editor gets.

- [x] **B20 [medium]** `frontend/src/components/ServiceModal.tsx:538-541,595-599` · The Plex-library
  and instance pickers have no error branch, so a failed fetch renders an empty list under the
  assertion "No Plex libraries yet. Sync them in Plex settings first." The operator is told as fact
  that they have no libraries and sent to re-sync a list that already exists. Rules 17/36. **Fix:**
  add `.error` branches rendering `.notice.notice-warn` ("Couldn't read your Plex libraries. Try
  again."), keeping the "none yet" sentence for the genuinely-empty, non-error case.

- [x] **B21 [medium]** `frontend/src/components/ServiceModal.tsx:175` vs
  `frontend/src/components/PlexPanel.tsx:241` · The same resource is cached under two keys,
  `["plexLibraries"]` and `["plex-libraries"]`, and `invalidateAllPlex`/`setQueryData` only ever touch
  the hyphenated one. Add a library in Plex, press Sync, then open a Radarr instance inside the 30s
  staleTime and the new library is missing from the dropdown; a disabled one is still offered. Only a
  reload clears it. **Fix:** use `["plex-libraries"]` in both files and delete the other spelling,
  grepping the key before closing (rule 64). The near-miss pair `["vocabulary-values", kind]`
  (`ReviewQueue.tsx:2278,2283`) and `["vocab-values", field.key]` (`PolicyEditor.tsx:766`) should be
  unified in the same pass.

- [x] **B22 [medium]** `frontend/src/api.ts:1116-1117` (same class at :1158, :1335) · `request()`
  guards the parse against an empty body but not a malformed one, so a non-JSON 200 throws a raw
  `SyntaxError` rather than an `ApiError`. A forward-auth proxy whose SSO session expires answers with
  a 200 HTML login page: `response.ok` is true, `JSON.parse` throws, every `error instanceof ApiError`
  branch falls through to its generic arm, and surfaces that render `error.message` show parser
  jargon. **Fix:** wrap `JSON.parse` in try/catch and rethrow as
  `new ApiError(response.status, "Reaper got an unexpected reply from the server.")`; do the same for
  the uncaught `response.json()` in `api.candidates` and `api.restorePrepare`.

- [x] **B23 [medium]** `frontend/src/App.tsx:706-717,822-825` · Every masthead tab click clears
  `settingsFocus`, and `<Settings>` is keyed on `settingsFocus?.nonce ?? "settings"`, so clicking the
  tab you are already on force-remounts the whole Settings subtree and destroys unsaved input. Arrive
  via a "Run one from Settings → Jobs" link, type a new application name, click "Settings" in the top
  nav, and the typed values are gone and the panel resets to General, with no warning. **Fix:** skip
  the focus resets when `n.id === view`, or drop the `key` remount and let `Settings` react to the
  nonce via an effect the way `PolicyEditor` consumes `focus`.

- [x] **B24 [medium]** `frontend/src/components/ReviewQueue.tsx:1056` · `CardStatusLine` falls back to
  `<StatusChip chip={chip}/>` for any non-condemned card, but a condemned row's `chip` is `null` by
  construction (`_chip` returns None for verdict "condemn"). Spare a condemned movie and
  `patchInPlace` sets `override: "spare"` with the chip untouched, so `isCondemned` goes false, the
  dormancy pill and reason both vanish, and the card silently loses a line and reflows under the
  cursor — the live reflow the in-place patch exists to prevent. **Fix:** when `!condemned` and `chip`
  is null, still render `<DormantPill dormantFor={dormantFor}/>`; the dormancy fact is true on every
  lane.

- [x] **B25 [medium]** `frontend/src/components/ReviewQueue.tsx:2143-2146` with `api.ts:1393-1394` ·
  The `reapNow` mutation forwards the selection straight to `api.createRun(keys)`, which collapses an
  empty array into an omitted field, and an omitted field means "the whole condemned set". Rule 1, on
  the deletion path, guarded today only by a `disabled` attribute. Any future filter on the selection
  that yields `[]` silently escalates a no-op into a whole-library plan. **Fix:** fail closed in
  `mutationFn` (`if (keys.length === 0) throw new Error("Nothing is selected.")`) and give
  `api.createRun` an explicit whole-set caller shape so `[]` can never read as "everything".

- [x] **B26 [medium]** `frontend/src/components/PolicyEditor.tsx:2354` · The `max_unmeasured_per_run`
  warning is anchored beside the pace control that sets it, but the server computes it from
  `active_profile_settings` (the SAVED profile), never the drafted value on screen. Drag it from 5 to
  0 and the warning under the box keeps saying Reaper will delete up to 5; raise it 0 to 5 and no
  warning appears until after the save. **Fix:** send the pace draft with the validate call, or
  compute this one warning client-side from `pace.max_unmeasured_per_run` and drop it from the
  server's `inspect` output. Do not leave it anchored to a control it does not track.

- [x] **B27 [medium]** `frontend/src/components/Fairness.tsx:212-247` · The "Not in the last scan"
  tile — the one affordance that explains why Scales is empty — is nested inside the
  `data.rows.length > 0` branch, so it is hidden exactly when it is needed. With a fresh portal or
  unbackfilled ids, `rows` is `[]` and `not_in_scan` is 40, and the page shows only "No available
  requests are in the last scan yet."; the 40 unmatched requests and the button that explains them
  are suppressed. **Fix:** move the `not_in_scan > 0` tile out of the `rows.length > 0` block.

- [x] **B28 [medium]** `frontend/src/components/Fairness.tsx:46-49` vs
  `frontend/src/components/ScalesPanel.tsx:206-209` · The card divides by `requests_made`, the panel
  it opens divides by `requests_in_scan`, and the two are not the same set: `requests_made` counts
  every matched group including a season-scoped request that scoped to nothing, which the detail
  builder explicitly skips. The same person, same scan, reads "4 requests · 50% they watched" on the
  card and "3 requests in the last scan · 67% They watched" in the panel. Rule 30. **Fix:** skip the
  `requests_made` increment in the roll-up when `scoped` is empty, exactly as the detail builder does,
  and cover it with a test using a season-scoped request whose seasons are absent.

- [x] **B29 [medium]** `frontend/src/components/PolicyEditor.tsx:1703-1707` · `paceClause` falls back
  to "removes only within your caps" whenever `pace` is null, which includes the case where the
  profile query *failed*. The intent band then asserts caps are in force directly above a section that
  says "Couldn't load these settings." Rule 53. **Fix:** branch the fallback on `paceFailed` and drop
  the pace clause entirely when the profile could not be read, keeping the neutral wording only for
  the still-loading case.

- [x] **B30 [low]** `frontend/src/components/ReviewQueue.tsx:1993-2000` · A tab's remembered filters
  are adopted in an effect, so for one commit the new `verdict` is paired with the previous tab's
  `filters` in the `useInfiniteQuery` key. Switching from Condemned (Genre=Drama) to Sanctuary fires
  `?verdict=protect&genre=Drama`, renders it, then fires the correct request: one wrong list flashes
  and the server does double work on every such switch. **Fix:** adjust the state during render
  (React's supported props-changed pattern) with a `filtersVerdict` ref, leaving the effect
  responsible only for `saveFilters`.

- [x] **B31 [low]** `frontend/src/components/PolicyEditor.tsx:1771-1789,2410-2413` · Discard does not
  clear `pendingSwitch`, so the amber "You have unsaved movie policy changes. Switching to TV discards
  them." notice survives the discard that made it untrue, still offering a red "Discard and switch"
  for changes that no longer exist. **Fix:** `setPendingSwitch(null)` in the Discard handler, or clear
  it in an effect when `dirty` goes false.

- [x] **B32 [low]** `frontend/src/components/PolicyEditor.tsx:935` · A ramp rule silently rewrites the
  operator's bound: `saturate_at: Math.max(rFrom + 1, rTo)` turns "from 5 years to 1 year" into 1826
  days, so the row reads "(from 5 years to 1826 days)" — a step function nobody asked for, added to
  the removal lane. **Fix:** validate before adding — disable "Add rule" with a one-line `.help` while
  `rTo <= rFrom` — rather than clamping.

- [x] **B33 [low]** `frontend/src/components/Settings.tsx:570-574` · The API-key notice renders
  `reveal.error ?? generate.error ?? copy.error`, and a mutation's error survives until its own next
  call, so a stale failure keeps rendering beside a now-working key: fail Copy on a plain-http LAN
  page, then succeed at Show, and the red notice is still there. **Fix:** call `reset()` in each
  mutation's `onMutate`, or render only the most recently settled mutation's error.

- [x] **B34 [low]** `frontend/src/accent.ts:44-49` vs `frontend/index.html:41` · The pre-paint script
  re-implements `accentInk` with a rounded luminance constant (`0.012`) where `accent.ts` computes
  0.0125359, so the two disagree for a band of accents near L≈0.2055 (for example `#009050`):
  index.html pre-paints `--accent-ink: #06202c`, then accent.ts sets `#ffffff`, and the ink on every
  accent-filled button flips at first paint — the exact flash the pre-paint exists to prevent. The
  comment there claims the math must match. Rules 67/68. **Fix:** have `applyAccent` cache the
  computed ink in localStorage beside the accent (the pattern `FAVICON_STORAGE_KEY` already uses) and
  let index.html read it back, so the math lives in one place.

- [x] **B35 [low]** `frontend/src/components/DeletionToggle.tsx:22-25` · Arming and disarming
  invalidate `["health"]`, a key no component uses — a leftover from the removed health-based safety
  read, whose retirement `App.tsx:49-51` documents. A reader auditing what arming refreshes is told a
  cache is kept in step that does not exist. **Fix:** drop the line (rule 64).

- [x] **B36 [low]** `frontend/src/index.css:7584-7592` · `.docs-index-item:hover` and
  `.docs-index-item.active` are equal specificity with `.active` declared later, so the doc you are
  reading swallows its own hover state and there is no `.active:hover` to deepen it. Milder than B13
  but the same missing companion rule (47). **Fix:** add
  `.docs-index-item.active:hover { background: color-mix(in srgb, var(--accent) 22%, var(--surface)); }`.

## 3. Hacks and workarounds

- [x] **H1 [medium]** `frontend/src/components/StatusChip.tsx:39-52` · `BLOCKED_WHY` and the
  `"Kept · "` prefix parse hard-code the backend's exact operator prose as lookup keys, so the
  frontend re-parses display copy it does not own, with no test or type tying the two sides together.
  Reword one backend chip and `chipWhy` returns null: every held-reap chip and `seasonDivergence`
  reason silently degrades to the generic "Reaper couldn't confirm it's safe to remove", and the
  frontend tests stay green because they hard-code the same string client-side. **Fix:** send the
  clause from the server as a field on `ChipOut` (`why: str | None`, produced beside `text` in
  `_chip`) and reduce `chipWhy` to reading it. If that is too large, add a backend test asserting the
  produced chip texts are exactly the keys of a shared constant the frontend map is generated from
  (rule 68's generator-plus-drift-test shape).

- [x] **H2 [low]** `frontend/src/App.tsx:658` · The j/k/Escape queue-navigation handler detects "a
  modal is up" with `document.querySelector('[role="dialog"]')` — a live DOM probe standing in for
  state React already owns, run on every keypress. Any future overlay that is modal but not marked
  `role="dialog"` (or marked but non-modal, like a popover) silently gains or loses keyboard capture.
  **Fix:** track modal depth in the existing `BackNavProvider`, which already knows the layer stack,
  and read that.

- [x] **H3 [low]** `frontend/src/components/PolicyEditor.tsx:921` · An
  `eslint-disable-line react-hooks/exhaustive-deps` with no explanatory comment, unlike the four other
  disables in the codebase (`ServiceModal.tsx:203,265`, `ReapConfirm.tsx:98`), which each carry a
  one-line reason. CLAUDE.md makes the two react-hooks rules errors, so a bare suppression is the one
  thing a reviewer cannot check. **Fix:** add the reason (the effect reseeds the rule form when the
  chosen field changes and must not re-run when the derived values do), or restructure so the rule
  passes. The twin at :1145 needs the same treatment.

## 4. Refactor opportunities

- [ ] **R1 [medium]** · *Mostly done in batch 7.* Landed: (a) `reviewFate.ts`, (b)
  `components/OverrideControls.tsx`, (e) `components/queueIcons.tsx`, plus
  `components/queueFilters.tsx` (the data half of (c)) and `components/queueSettings.tsx` (the
  shared subscription P-7 added). `ReviewQueue.tsx` is 2,962 -> ~2,360 lines and the panels no
  longer import their safety helpers out of a page component. **Still open:** the filter
  toolbar's JSX with `Pill`/`FilterChip` (the rest of (c)) and `useCardSelection` (d). Both are
  restructures rather than moves -- the toolbar needs ~15 props threaded to a new component --
  so they were left rather than rushed. Original finding: `frontend/src/components/ReviewQueue.tsx:1-2962` · One module holds the fate
  primitives three other files import, the whole filter subsystem, the override controls, both card
  shapes, and the queue container, so every reviewer of a two-line rule-48/49 change reads 3000 lines,
  and `WhyPanel`/`ShowPanel` import their safety helpers out of a page component. **Fix:** extract
  along seams that are already self-contained: (a) `reviewFate.ts` — `Fate`, `handFate`,
  `isCondemned`, `reapIsNoop`, `showReapIsNoop`, `groupReapEffective`, `showReapReach`,
  `useHoldsBackUnmeasured` (403-1113); (b) `components/OverrideControls.tsx` — `OverrideControls`,
  `SpareMenu`, `SPARE_PRESETS`, `OverrideMark`, `KeptByShowNote`, `useDefaultSpareDays` (496-947);
  (c) `components/QueueFilterBar.tsx` — `QueueFilters`, `DEFAULT_FILTERS`, `loadFilters`/`saveFilters`,
  the option lists, `Pill`, `FilterChip`, the toolbar JSX (60-345, 2314-2736); (d) `useCardSelection.ts`
  (949-961, 2226-2271); (e) `components/queueIcons.tsx` for the twelve inline SVGs. Keep re-exports
  from `ReviewQueue.tsx` for one commit so test imports do not churn.

- [ ] **R2 [medium]** · *Partly done in batch 7.* Landed: `policyPresets.ts` (the presets,
  the shipped mixes, the rescale arithmetic and the preset test) and `PolicyRuleEditors.tsx`
  (both rule editors and their shared vocabulary plumbing). `PolicyEditor.tsx` is 2,704 ->
  ~1,830 lines. **Still open:** the `usePolicyDraft` / `usePaceDraft` / `usePolicyWarnings`
  hooks and the four memoized section components. Note for whoever takes it: `builtInWeight`,
  `yourWeight` and `activePreset(draft)` all sit AFTER the `if (!draft)` early return, so they
  cannot become hooks where they stand -- extracting the sections is what makes them
  memoizable. Original finding: `frontend/src/components/PolicyEditor.tsx:1408-2479` · The exported component is
  a single 1,071-line function with eleven pieces of state, nine queries/mutations, six effects, and
  the markup for four sections, none of it memoized, so one keystroke in any field re-runs
  `activePreset`, both `rescaleToBudget` inputs, `warningsFor` over every warning, and the intent
  summary, and re-renders all four cards and both rule editors. **Fix:** extract `usePolicyDraft()`,
  `usePaceDraft()`, and `usePolicyWarnings()` hooks, then `<FlagsSection>`, `<KeptSection>`,
  `<PaceSection>`, and `<PolicySaveBar>` as memoized components; move the presets block to
  `policyPresets.ts` beside `policyMeta.ts` and the rule editors to `PolicyRuleEditors.tsx`.

- [x] **R3 [medium]** `frontend/src/api.ts:1133-1168,1275-1295,1300-1320,1325-1336` · Four call sites
  (`candidates`, `downloadLogs`, `downloadBackup`, `restorePrepare`) hand-roll `fetch` plus the
  `!response.ok` + `reason()` block instead of going through `request()`, so the wrapper is not the
  single choke point it appears to be: any cross-cutting change (the 401 hook in PR2, the malformed
  body guard in B22, a retry, a timeout) silently misses a quarter of the surface including the
  queue's own paging call. They have already diverged — `request()` tolerates an empty body, the
  copies do not. **Fix:** extract the ok/`reason()` block into one `checkResponse(response)` helper
  and have all four call it.

- [x] **R4 [medium]** `frontend/src/index.css:93,1333,1336,2453,2456,2539,2549,2557,2561,2565,2831,3148,4371,4418,4677,5982` ·
  About 90 lines of CSS match no element: `.title-cell`, `.why-cell`, `.conditions`, `.condition-list`,
  `.rules-row-head`, `.effect-pill`/`.effect-remove`/`.effect-keep`, `.rules-weight-cell`,
  `.savebar-hash`, `.spare-cell`, `.card-summary`, `.spare-btn`, `.chip-spared`, `.plex-status`,
  `.btn-plex`. `.caps-grid` is named in the control-standard comment at :93 as a compliant class but
  has no rule and no markup at all. `.spare-btn` is a near-twin of the live `.stop-btn`, which invites
  a future fork. **Fix:** delete the listed blocks and drop `.caps-grid` from the standard's comment
  (or add the class, if the caps grid was meant to carry it).

- [x] **R5 [medium]** `frontend/src/index.css:3404-3420` · `.fair-stat-btn`'s hover, focus-visible,
  selected, and selected-hover rules are byte-for-byte copies of `.card`'s four (4248-4275), whose
  comment asserts the grammar "is declared once here ... so a card-hover tweak lands in one place". A
  future change to that grammar lands on `.card` and silently skips the Scales tiles — the drift the
  comment claims is impossible. A third unexplained wash value already exists at :3731 (6% where
  siblings use 7%). **Fix:** add `.fair-stat-btn` to the four existing grouped selectors, delete
  3404-3420, and normalize the 6% or state why it differs.

- [x] **R6 [low]** `frontend/src/api.ts:1422-1444` · `api.whitelist`, `api.spare`, and `api.unspare`
  have no callers anywhere in the frontend (verified by grepping every `api.*` usage): three uncalled
  copies on the keep-list, a safety-adjacent path, where a reader cannot tell which one the UI
  actually uses (`api.override`/`api.clearOverride`). Rule 38. **Fix:** delete the three methods; the
  routes stay for the API-key lane and `WhitelistEntry` is still `api.override`'s return type.

- [x] **R7 [low]** `frontend/src/index.css:8054` · `@media (max-width: 720px)` is the only width query
  off the documented 1100/900/640/560 grid, reintroducing the one-off breakpoint the previous review
  removed. The app now has two nearly-identical narrow breakpoints, so a change made at 640px silently
  misses the docs modal. **Fix:** move to 640px, or keep 720 and add the justifying comment the 560px
  block models.

- [x] **R8 [low]** `frontend/src/index.css:1797-1817` vs `frontend/src/App.tsx:466-470` · Rule 67 is
  half-applied on the 900px full-screen-sheet coupling: `App.tsx` carries the cross-reference, the CSS
  block it points at carries no back-reference, so someone editing the media query has nothing telling
  them `useMediaQuery("(max-width: 900px)")` must move with it. **Fix:** add one line to the CSS
  comment naming `App.tsx`'s `fullSheet` query.

- [x] **R9 [low]** `frontend/src/index.css:1511,1716` · `.show-reason` and `.rating-chip` still
  hardcode `border-radius: 8px`, the two survivors of the sweep that replaced the other 8px/7px
  literals with `var(--radius-sm)` (7px), so they render 1px rounder than every sibling and will
  detach outright if the token changes. **Fix:** replace both with `var(--radius-sm)`.

## 5. Performance

- [x] **P1 [medium]** `frontend/src/components/ReviewQueue.tsx:2274,2389-2391,2156-2158` ·
  `toGroups(data)` and every derived array (`shownGroups`, `shownKeys`, `shownItems`,
  `allShownSelected`) are recomputed on every render with no `useMemo`, and `onSet`/`onClear`/
  `cardSelect` are freshly allocated, so no card can ever be memoized. Drag-selecting across a list
  scrolled to ~400 cards runs one full re-render per `pointerenter`, each re-folding every fetched
  candidate into groups and re-rendering every drawn card. **Fix:** `useMemo` the fold on `[data]`,
  memoize `shownGroups`/`shownKeys` on `[groups, visible]`, `useCallback` the handlers, and wrap
  `MovieCard`/`ShowCard` in `React.memo` (the `select` object must then be memoized too).

- [x] **P2 [medium]** `frontend/src/components/ReviewQueue.tsx:1743-1751,1898-1907` · With "Expand
  seasons by default" on, every drawn `ShowCard` mounts a `SeasonList` that fires its own
  `useQuery(["group", groupKey])` with no `staleTime` — one request per card, unbounded as the render
  window grows, and entering or leaving Select mode unmounts and remounts every list, firing them all
  again. **Fix:** give the group query a `staleTime` (the sibling vocabulary queries use 5 minutes)
  and gate auto-expansion on the render window, or fetch season rows on first paint into view.

- [x] **P3 [medium]** `src/reaper/api/runs.py:204-216` with `:92-124` · `GET /api/runs` calls
  `_run_out` per run, and each call re-reads the whitelist, the profile, and the entire condemned
  candidate set of that run's snapshot. `ReapPlan` mounts this query on every visit and after every
  reap, so with the default `limit=50` the same thousands of ORM rows are loaded dozens of times per
  page load. **Fix:** give the history a light shape (`RunSummaryOut`: id, state, approved_at, the
  stored `held_back_unknown_size`), or memoize `whitelist.overrides` + `effective_condemned` per
  `snapshot_id` across the loop. As a bonus this stops a finished run's phrase being recomputed
  against today's overrides.

- [x] **P4 [medium]** `frontend/vite.config.ts` / build output · The app ships as a single 542 kB JS
  chunk (157 kB gzipped) with no `React.lazy` or `Suspense` anywhere in `frontend/src`, so every first
  load pays for the policy editor, the settings panels, the docs content, and the simulator before the
  review queue paints. **Fix:** lazy-load the route-level surfaces that are not the landing view —
  `Settings`, `PolicyEditor`, the docs modal content, `PolicySimulator` — behind `React.lazy` with a
  `Suspense` fallback matching the existing spinner.

- [x] **P5 [low]** `frontend/src/components/ScanBar.tsx:129` · The scan-finished effect calls
  `queryClient.invalidateQueries()` with no filter, refetching every mounted query in the app: the
  logs, the schedule, safety, profile, every settings panel, and every already-loaded page of the
  infinite `["candidates"]` query at once, on a server that has just finished a full scan. **Fix:**
  name the caches that actually hang off the snapshot — `["snapshot"] ["candidates"]
  ["reap-breakdown"] ["runs"] ["fairness"] ["schedule"]`.

- [x] **P6 [low]** `frontend/src/components/LogsPanel.tsx:83-90` · `visible` is recomputed inline on
  every render, lowercasing up to 2000 lines per pass, and re-renders on each 2s poll and each search
  keystroke. **Fix:** `useMemo` on `[lines, search, minLevel]`, and pre-lowercase `line.text` once when
  folding a page into `_logStore` rather than per filter pass.

- [x] **P7 [low]** `frontend/src/components/ReviewQueue.tsx:490-493,1035-1038` · `useDefaultSpareDays`
  and `useHoldsBackUnmeasured` each open a `useQuery` observer per row — one per `OverrideControls`
  (card plus every season row) and one per `CardStatusLine` — so 400 drawn cards with expanded season
  lists create roughly a thousand observers on two keys. The request is deduped, but every cache write
  re-renders all of them. **Fix:** read both once in `ReviewQueue` (which already reads
  `general-settings`) and thread the values down as props, keeping the hooks only for the standalone
  panels.

- [x] **P8 [low]** `frontend/src/components/ReviewQueue.tsx:728-753` · `SpareMenu`'s listener effect
  depends on `onClose`, which `OverrideControls` allocates fresh every render, so three
  document/window listeners are torn down and re-added on every render while the menu is open,
  including on every keystroke in the Custom-length box. The file already avoids this for `custom` via
  `customRef`. **Fix:** hold `onClose` in a ref and drop it from the dependency array, or `useCallback`
  the handlers with empty deps.

- [x] **P9 [low]** `frontend/src/components/ReapPlan.tsx:37-57` · The plan's step table renders every
  journalled step with no cap, while the dry-run outcome list directly above caps at 50. A 500-item
  first cleanup is 1500 `<tr>` rows, each with a `<code>` path and a stringified JSON body, rendered
  synchronously on plan build and again on every history-row click. **Fix:** slice the way `Report`
  does — first 50 plus an "and N more" line — or paginate the table.

## 6. Production readiness

- [x] **PR1 [high]** `frontend/src/App.tsx:118-121,177-184` · The app-wide reap bar's Stop is a
  mutation with no rendered error state, so a failed stop is silently swallowed: the operator presses
  Stop mid-reap, the POST fails, `isPending` clears, the button re-renders live, and nothing on screen
  changes, so they believe the deletion is halting while it keeps deleting. The same control in
  `ReapConfirm.tsx:229` renders its error. Rule 36. **Fix:** render `stop.error` as
  `.notice.notice-error` beside the button in `ReapBar`, mirroring `ReapConfirm`.

- [x] **PR2 [medium]** `frontend/src/api.ts:1101-1118` · There is no global 401 handling, so a revoked
  or expired session is reported per-panel and the app never returns to the login gate. The restore
  flow is the concrete case: the confirmed restore swaps the database and the operator restarts the
  container, the restored DB carries different session rows, and the open tab's cookie is dead, but
  `["me"]` is never refetched, so the SPA stays on the Dashboard with every query rendering its own
  "Not authenticated." and no route back to `Login`. The 30-day session TTL hits the same wall.
  **Fix:** add one 401 hook in `request()` (and the three hand-rolled fetch sites, see R3) that clears
  `["me"]` via an injected client so a 401 drops the app back to `Login`.

- [x] **PR3 [medium]** `frontend/src/components/PolicyEditor.tsx:893-897,1120-1124` · Both rule editors
  take only `data` off their vocabulary query, so a failed fetch renders an empty field picker with no
  error and no retry, and `RemoveRulesEditor` looks like a feature with nothing to configure. The
  operator concludes "there are no fields", not "the fetch failed". Rule 36. **Fix:** destructure
  `error`/`isPending` from both queries and render `.notice.notice-error` with a plain-language lead
  in place of the empty select, matching the treatment the policy and profile queries already get in
  the same file.

- [x] **PR4 [medium]** `frontend/src/components/DeletionToggle.tsx:42-48` · When the safety query
  fails, the component renders only the amber "treat it as on" notice and removes the Turn-off control
  — the one direction the backend never gates. An operator who wants to put Reaper back to read-only
  right now is offered no button at all, only advice to assume it is armed, and must reload until a
  GET succeeds. **Fix:** keep the amber unknown notice but still render "Turn off" in the error branch
  (`toggle.mutate({ enabled: false })` needs no password and no prior state), so the safer direction
  is always one click.

- [x] **PR5 [medium]** `frontend/src/components/PolicyEditor.tsx:2289-2304,2343-2350` · Pace number
  boxes carry no `max`, so out-of-range values reach the API and return a raw 422 rendered verbatim.
  `max_unmeasured_per_run` is `le=25` server-side but the input has no ceiling and the savebar's
  disable clause covers policy validity only; the help text never states the bound. **Fix:** add
  `max={25}` / `max={1000}`, clamp on commit, and state the bound in the help text so the limit is
  visible before the round trip.

- [x] **PR6 [medium]** `frontend/src/components/Settings.tsx:1939-1945` · No password or username input
  carries a `maxLength`, but every server field is `Field(max_length=128)`, so pasting a
  130-character passphrase from a password manager leaves Save enabled (only the 12-character floor is
  checked) and answers "The password wasn't set: String should have at most 128 characters" —
  validator wording for a rule the UI never stated. **Fix:** add `maxLength={128}` to the password and
  username inputs in `AdminPasswordForm`, `LocalSheet`, `RestoreCard`, and `DeletionToggle` (and
  `maxLength={256}` to the recovery code).

- [x] **PR7 [medium]** `frontend/src/components/PolicyEditor.tsx:2417-2428` · One Save button gates two
  deliberately independent saves, so a policy that is off the 100-point budget also blocks the pace
  save that has nothing to do with it: drag a weight so `pointsLeft = -5`, then edit the grace period,
  and the grace change cannot be saved until the point budget is fixed. This contradicts the file's own
  header ("tightening a cap never voids an approval") and the savebar's "Two independent saves"
  comment. **Fix:** keep one save affordance (rule 43) but gate per half — enable when either half is
  savable, skip the blocked half in `onClick`, and have `savebar-what` say which half is held back and
  why.

- [x] **PR8 [medium]** `frontend/src/components/ReapPlan.tsx:139-166` · The Reap page never tells the
  operator the last scan came back incomplete: it renders a full ledger and a Build button for a
  snapshot the planner will refuse outright, and the 422 arrives only after the click, as a bare
  notice. `ReapPlan` already fetches `["snapshot"]` and reads only `.id`. **Fix:** read
  `latestSnapshot.degraded`/`.degraded_reason`, render the same `.notice.notice-warn` wording
  `ScanRow` uses, and disable Build while it is set.

- [x] **PR9 [medium]** `frontend/src/brand/deepIcon.ts:6-13` with `deepIcon.test.ts:49-62` · Rule 68 is
  unmet: the comment says the five committed PNGs are rasterized from the SVG variants but names no
  committed, runnable generator (none exists in `scripts/` or as an npm script), and the drift test
  only reads each PNG's IHDR width and height. A brand change regenerates `favicon.svg` — the vector
  test forces that — while all five PNGs keep the old drawing and still pass, because their dimensions
  never change. The test's own comment claims it catches exactly this, which a size check structurally
  cannot do. **Fix:** commit `scripts/gen-icons.mjs`, name it in both comments, wire it as
  `npm run icons`, and replace the size assertion with a content check.
  **Landed** with the brand-mark change (the module is now `appIcon.ts`, its test `appIcon.test.ts`):
  `frontend/scripts/gen-icons.mjs` writes all six assets plus `src/brand/icons.generated.json`, which
  records the sha256 of the exact SVG string each asset was rasterized from. The test re-derives that
  string from the current `appIconSvg` and compares, so a redraw that skips `npm run icons` fails by
  asset name; a seventh test cross-checks the generator's asset list against every icon `index.html`
  and `site.webmanifest` actually reference, so neither side can gain an entry alone.

- [x] **PR10 [medium]** `frontend/src/components/ReapConfirm.tsx:51-55` and `frontend/src/App.tsx:113-117` ·
  The `["reapStatus"]` query stops polling entirely when nothing is running, in both consumers, so a
  reap started elsewhere never surfaces in an already-open, focused tab: the app-wide bar stays dark
  and the Reap page keeps offering Execute until the tab reloads or the sheet is opened — and Stop is
  only reachable from that bar. The scan line idle-polls at 15s for precisely this reason. **Fix:**
  `refetchInterval: (q) => (q.state.data?.running ? 1000 : 15000)` in both places.

- [x] **PR11 [low]** `frontend/vite.config.ts:25` · `build.sourcemap: true` ships full source maps from
  the production Docker build with no comment saying that is intended, inflating the image and the
  first-load transfer, while the neighboring proxy block is heavily commented about why it is safe.
  **Fix:** state the intent beside it, or switch to `sourcemap: "hidden"` so stack traces stay
  resolvable in CI without shipping the maps.
  **Done (batch 10): the intent is stated, the maps stay.** Two of the reasons to drop them do
  not hold here. First-load transfer is unaffected: a browser fetches a `.map` only with devtools
  open, so a normal load carries the `sourceMappingURL` comment and nothing else. And the sources
  are AGPL and published, with no build-time value in the bundle (`src/` has no `import.meta.env`
  use at all), so there is nothing to withhold. What is left is ~2 MB of `.map` files in the
  image, against an operator's console being the only debugger we get on a server we will never
  see. The comment says all of that, and names `"hidden"` as the fallback if the tradeoff shifts.

## 7. UI/UX consistency

- [x] **U1 [high]** `frontend/src/index.css:47` and ~21 use sites (331, 555, 1227, 1675, 1755, 1785,
  3142, 3426, 4467, 4529, 4631, 4667, 4671, 5268, 6045, 6895, 7518, 7590, 7647, 7771, 7827) · The
  default accent `#25c3ff` is used as *text* ink and fails WCAG AA badly in the light theme: 2.03:1 on
  `--surface`, 1.87:1 on `--bg`, and 1.81:1 on `--accent-soft` (dark mode passes at 8.47:1). That
  covers every `<a>` and `button.link`, `.chip-requested`, `.chip-tv`, `.fchip`, `.filter-chip`,
  `.rule-tag`, `.jump-pill`, `.docs-index-item.active`, `.doc-kicker`, and `.log-lv.info`. The file
  already computes contrast for `--faint`, `--muted`, and the verdict tokens, and `accentInk`
  documents that white would fail *on* the accent — the reverse direction was never checked. **Fix:**
  mint an `--accent-text` token (light: `color-mix(in srgb, var(--accent), #000 42%)`, tuned to clear
  4.5:1; dark: `var(--accent)`), point every `color: var(--accent)` that carries readable text at it,
  and extend `accent.ts`'s `accentInk` to derive it for custom accents. Leave `border-color` and
  `background` uses alone.

- [x] **U2 [high]** `frontend/src/index.css` — 18 sites including 3268, 3384, 3683, 3704, 3785, 3818,
  3865, 3877, 3928, 4610, 5224, 6040, 6155, 6685, 7568, 7595, 7629, 8044 · `--faint` carries real text
  against its own token comment at :27-30, which states outright "Never put text on --faint" and
  documents that it clears no AA ratio in either theme (light 2.30-2.62:1, dark 3.17-3.52:1). Affected
  readable strings include `.rb-of` ("of 12 titles"), `.jobrow-sched`, `.jobrow-last.is-never` ("never
  run"), `.nis-noname`, `.scales-title-meta`, `.dz-hint`, the entire docs index (`.docs-index-h`,
  `.docs-index-item small`, `.docs-index-foot`), and `.dd-phase`. The previous review fixed the
  then-known uses; every surface built since re-adopted the token. **Fix:** switch all 18 to
  `var(--muted)` (5.33:1 light, 6.65:1 dark), keeping `--faint` only for the genuinely decorative uses
  (chevrons, the search glyph, the poster placeholder mark, the unticked setup disc, a disabled
  number).

- [x] **U3 [high]** `frontend/src/components/PolicyEditor.tsx:1819-1826` · The TV intent summary
  asserts two protections unconditionally: "anyone's mid-binge" ignores `draft.keep_in_progress`, and
  "always keeps the newest N seasons" prints even at N = 0. Turn both off and the one-line read of the
  whole policy still says "always keeps the newest 0 seasons of a show and anyone's mid-binge" — both
  false, on the line an operator scans before arming. Rules 53 and 61. **Fix:** build the TV clause
  the way `keepClauses` (:1696-1699) is built for movies: push each clause only when its switch is on,
  and extract it as `tvKeepClauses` beside `keepClauses` so both media types go through one
  construction.

- [x] **U4 [medium]** `frontend/src/index.css:7235-7238` ·
  `.reap-confirm-input:focus { outline: none; border-color: var(--condemn); }` is the only
  `outline: none` in the file with no ring replacement — on the one field that arms a real deletion.
  At 0-2-0 it beats the global `:focus-visible` rule (0-1-0), so a keyboard user tabbing in gets no
  ring at all, only a 1.5px border recolor. Every other `outline: none` here is paired with a
  `:focus-within` ring on the composite parent or an explicit box-shadow. **Fix:**
  `outline: 2px solid var(--accent); outline-offset: 1px;` alongside the red border.

- [x] **U5 [medium]** `frontend/src/index.css:737-753` · `.auth-aurora` runs a 22s infinite
  translate-and-scale animation on a full-bleed blurred layer with no `prefers-reduced-motion` opt-out,
  while seven reduced-motion blocks in the same file disable far smaller motions. A motion-sensitive
  operator gets a permanently drifting background on the sign-in screen, the first and unavoidable
  surface (WCAG 2.2.2 applies). **Fix:** add `.auth-aurora { animation: none; }` to the nearest
  reduced-motion block; the static gradient keeps the depth.

- [x] **U6 [medium]** `frontend/src/index.css:4568-4580,4473-4481,2750-2758` · The removable-chip ×
  buttons are about 16×16 CSS px, under the WCAG 2.2 SC 2.5.8 24×24 minimum, and each sits flush
  against its sibling target so the spacing exception does not apply: on the filter bar `.fchip-x`
  (clear this filter) is immediately adjacent to `.fchip-body` (edit this filter) inside a ~20px chip,
  so a touch meaning "change the genre filter" lands on "remove it". Same geometry on
  `.filter-chip button` and the keep-tag `.tag-chip button`. `.bar-x` is already sized to exactly
  24×24, so the standard exists. **Fix:** give all three `min-width`/`min-height: 24px` with
  inline-flex centering and raise the chips' vertical padding to seat them.

- [x] **U7 [medium]** `frontend/src/components/PolicyEditor.tsx:1357-1371,2046` · `SeasonAdvisory`
  states how many shows are fully protected by keep-last-N without branching on
  `draft.keep_last_scope`, and the endpoint behind it counts every show in the snapshot regardless of
  scope. Set scope to "Requested only" and the figure directly above a destructive-pressure control —
  the operator's evidence that they have not over-protected — still counts every show. Rule 53.
  **Fix:** pass `scope` into `SeasonAdvisory` and either suppress the count for "requested" or have
  `/api/snapshot/season-shape` return a `requested_season_counts` map so the sentence derives from the
  exact set keep-last acts on (rule 30).

- [x] **U8 [medium]** `frontend/src/components/PolicyEditor.tsx:162-167,1666-1690,1844-1857` · Clicking
  a preset never lights that preset whenever the operator has any custom removal rule, because
  `applyPreset` rescales the built-in weights off the shipped mix while `activePreset` demands they
  equal it exactly. Click "Cautious" with one custom rule and the `Segmented` shows no active segment
  while the help line flips to "Custom: your own tuning, not a preset." on the very click that applied
  it, contradicted by the separate "Staged, not saved" line. **Fix:** have `activePreset` return
  `staged` when it is non-null, or compare against `rescaleToBudget(mix + current custom weights)` —
  the same transform `applyPreset` performs — instead of raw `DEFAULT_WEIGHTS`.

- [x] **U9 [medium]** `frontend/src/components/PolicyEditor.tsx:1965` · The server emits a
  `gates.server_popularity.window_days` warning whose advice ("A year is the usual setting") names a
  value the policy editor has no control for: `GateRow` edits only `enabled` and `threshold`, and
  `window_days` appears in the frontend exactly once, as a type. The operator is told to fix something
  with no fix on the page, and the gate's help text never defines what "recently" means. Rules 42 and
  25. **Fix:** add a `window_days` control to `GateRow` for `server_popularity` (a `QuantityInput` on
  `TIME_UNITS`, mirroring the dormancy row) so the warning anchors to its fix, and name the window in
  the help text.

- [x] **U10 [medium]** `frontend/src/components/ReapBreakdown.tsx:169-177` · The held-hand-reaps line
  names the wrong operation: "so a scan won't remove them yet", when a scan never removes anything and
  the Jobs page says so in the same product ("A scan only reads. It cannot delete."). What holds these
  back is a reap. **Fix:** "N reaps you marked are on hold, so this reap won't remove them yet."

- [x] **U11 [medium]** `frontend/src/components/Login.tsx:293-296` · The recovery card says the code was
  printed "to its log", but `mint_recovery_token` deliberately uses `print()` rather than the logging
  pipeline, so the code never reaches the in-app Logs tab or the downloadable log files. A locked-out
  operator follows the copy to Settings → Logs, finds nothing, and concludes recovery is broken.
  **Fix:** name the real place ("Reaper printed a recovery code to the container's console output"),
  keeping the sentence that explains why it is not in the URL.

- [x] **U12 [medium]** `frontend/src/docs/content/understandingPolicy.ts:57` · The docs tell operators a
  failed scan 'comes back "degraded"', quoting a word the UI never displays: `ScanBar` says "This scan
  came back incomplete." and `App.tsx` says "that scan came back incomplete". `degraded` is the
  internal schema field name, which rules 21 and 25 both bar from operator copy. **Fix:** 'If it comes
  back "incomplete," a source failed and Reaper marked the run unusable on purpose. Fix the source and
  scan again.'

- [x] **U13 [low]** `frontend/src/components/ReviewQueue.tsx:2827-2835,2914-2924` · The bulk bar counts
  CARDS ("3 selected") but the destructive button beside it acts on ITEMS: three show cards of ten
  seasons each say "3 selected" over a run covering up to 30 seasons. The confirmation sheet states the
  real count, so nothing unsafe ships, but the number beside a destructive button is not the set the
  server acts on (rule 30). **Fix:** sum `group.items.length` for selected show keys the way
  `shownItems` already does, and word it "3 cards · 30 items", or state items only.

- [x] **U14 [low]** `frontend/src/api.ts:1098` · The client's fallback error string is
  `Request failed (502).`, an HTTP status shown verbatim wherever a component renders `error.message` —
  behind a reverse proxy during a container restart that is what the review queue, the reap sheet, and
  every settings panel display. Rule 21. **Fix:** return plain language from `reason()` for the
  no-detail case ("Reaper couldn't reach the server. Try again.") and keep the status in a console log.

- [x] **U15 [low]** `frontend/src/docs/content/deletionSafety.ts:32` and
  `frontend/src/docs/content/cheatSheet.ts:48` · Two vocabulary drifts against copy the app already
  standardized: the flow node reads "Dry run" where the product says "practice run", and "Caps abort
  the whole run when crossed" contradicts `understandingPolicy.ts:156`'s "Caps stop the whole run when
  crossed" for the same mechanism. An operator meets "dry run" and "abort" only in the docs. **Fix:**
  "Practice run" / "a full rehearsal, nothing sent", and "Caps stop the whole run when crossed."

- [x] **U16 [low]** `frontend/src/index.css:8062` · `.docs-index { max-height: 32vh }` uses `vh` inside
  a modal explicitly sized in `dvh`, in a block that only applies below 720px — the devices where the
  two units differ. The `.modal` comment at :7141-7143 documents this exact hazard. **Fix:** `32dvh`;
  `.log-console`'s `max-height: 82vh` (:6860) has the same mismatch.
  **Done (batch 10), and swept.** Both named sites, plus two the finding missed: `.why`'s
  `calc(100vh - 2rem)` and `.why-loading`'s `min-height: 45vh`, the second of which renders inside
  the `.why` mobile sheet this file already sizes in `dvh`. No bare `vh` is left, so the rule is
  absolute now and enforced by `viewport-units.test.ts` rather than by a fourth comment -- this is
  the third time the same mismatch has been found and fixed.

- [x] **U17 [low]** `frontend/src/components/QuantityInput.tsx:58` · `QuantityInput` picks its display
  unit once, on mount, so a value replaced from outside — Discard, a preset, a media-type switch, the
  post-save re-seed — is shown in a unit that no longer suits it: a grace box mounted on "months"
  showing 2 becomes **0.23 months** when a preset stages 7 days. Arithmetically right, unreadable.
  **Fix:** track the last value emitted in a ref and re-derive the unit in an effect when the incoming
  value differs from it (rule 19's reset-on-identity-changing-props).

- [x] **U18 [low]** `frontend/src/components/PolicyEditor.tsx:1798-1807` · The section rail sets
  `aria-current="page"` and its comment claims "the section being read is stated, not just colored",
  but `activeSection` changes only on a rail click or a cross-page jump — there is no scroll observer,
  so scrolling to Deletion and arming leaves the rail marking "What flags a title" as current for
  sighted and assistive users alike. Rule 24. **Fix:** add an `IntersectionObserver` over the four
  `sectionRefs` headings, or correct the comment to say the rail states the last section jumped to.
  One or the other, in one change.

- [x] **U19 [low]** `frontend/src/components/ScalesPanel.tsx:53-57` · `limitText` pluralizes nothing, so
  a daily quota renders as "Movies 1 per 1 days", while the same file and `Fairness.tsx` already
  pluralize "person"/"people" and "title"/"titles". **Fix:** branch on `line.days === 1`.

## 8. Improvements

- [x] **I1 [low]** `frontend/src/components/ReapPlan.tsx:75-82` · The dry-run summary calls per-item
  outcomes "steps" and the comment above the list claims outcomes repeat per `media_key`, which the
  executor makes impossible (one `StepOutcome` per item). A plan of 3 TV seasons reports "3 steps were
  walked" over 9 journalled steps in the table below, and the headline number is a hardcoded zero by
  construction ("0 souls were actually reaped"), which tells the operator nothing about what the dry
  run proved. **Fix:** say what was walked — "Dry run complete. Every safety check ran and nothing was
  sent; N titles were walked end to end." — and drop the false comment at :80-82.

- [x] **I2 [low]** `frontend/src/components/SeasonList.test.tsx:302` · The queue's tests hard-code the
  backend's chip prose on the client side, which is why H1's drift is invisible to CI: both sides of
  the contract are asserted from the same transcribed string. When H1 lands, assert against the shared
  constant instead, so the two sides can never silently disagree again.

---

## Agent Rules

Direct constraints for the fixing agent and all future UI work. They extend CLAUDE.md rules
17-21/36/39-51/52-69 with the specific failure modes this review found. Treat each as a blocker, not
a suggestion.

1. **A confirmation the operator types is sent to the server verbatim.** Never post a client-held
   copy of a phrase, token, or checksum the server will re-derive: send what the human entered, and
   on a mismatch refetch the server's current value and re-seed the form. A gate the server cannot
   distinguish from an echo is not a gate.
2. **An item-level control asks the item, never the tab.** `hideReap`, fate coloring, and every other
   per-row decision route through the one shared helper (`reapIsNoop`, `showReapIsNoop`, `handFate`)
   applied to that row's own verdict and override. `verdict === "condemn"` written inline at a call
   site is a blocker; lane membership is the *effective* verdict and does not imply the stored one.
3. **A controlled numeric input keeps a local string buffer.** Never derive the displayed text from
   the stored value on every render, and never coerce `""` to a floor mid-typing: hold the raw text,
   emit only on a successful parse, commit the floor on blur. A `|| 1` at the call site is evidence
   the control is broken, not a fix.
4. **Every recovery notice that says "save" is accompanied by a reachable Save.** A fallback or
   recovery flag forces the dirty state for its own half of the form, and the test that asserts the
   notice also asserts the save affordance.
5. **An always-mounted safety surface polls.** Any query whose staleness could state the wrong safety
   regime carries an explicit `refetchInterval` and `refetchOnWindowFocus: true`; the global defaults
   are tuned for scan-driven data and are the wrong default for arming state.
6. **Prose and color both consult the effective decision.** Any sentence, count, chip, or pill
   asserting an item will be removed or kept branches on the override in effect (including
   `show_override` and held reaps), not on the scan verdict. If the card tints for a decision, every
   clause on that card moves with it.
7. **Every number on a page derives from one set.** Headline, ledger, split, and per-line counts all
   run over the exact set the server will act on, including the unknown-size hold-back. If one number
   subtracts something, all of them do, and the empty-state test uses the adjusted value.
8. **A completion side effect lives in a component that cannot be unmounted.** Cache invalidation
   after a long-running destructive job belongs on the always-mounted bar, fired once on the
   running-to-ended edge, not in a sheet the operator is invited to close.
9. **One resource, one query key.** Grep both spellings before adding a key; a mutation invalidates
   every key its data feeds, and a new consumer reuses the existing key rather than minting a variant.
10. **Never widen a destructive request by omission.** An empty selection throws at the mutation
    boundary; "all" is an explicit, separately-named caller shape. A `disabled` attribute is not a
    safety control.
11. **Every `useQuery` on a gating or state-claiming surface destructures `isPending` and `isError`
    and renders explicit fallbacks**, and every `useMutation` renders its error. An empty list from a
    failed fetch must never be phrased as "you have none".
12. **The safer direction of a safety control is always available.** Turning protection on, or
    deletion off, must not be gated behind a read that failed.
13. **A modal closes on a click that both started and ended on the scrim**, and refuses to close while
    a mutation it owns is pending (`canClose`).
14. **Poll for work started elsewhere.** Any status a second device or tab can start carries an idle
    `refetchInterval`, never `false`.
15. **Client-side bounds mirror the server's, visibly.** Every input whose server field has a
    `max_length`/`le` carries the matching `maxLength`/`max`, clamps on commit, and states the bound in
    help text. A validator message is never the first time the operator learns a limit.
16. **A `:hover` that can also be `.active` re-asserts the active treatment** at equal-or-higher
    specificity, remembering that `:not()` adds a class's weight. The `.ov-spare.active:hover` block is
    the reference implementation.
17. **Compute contrast before shipping a color as text**, in light and dark, against every ground it
    lands on (4.5:1 body, 3:1 large and UI glyphs). `--accent` and `--faint` are not text colors;
    `--accent-text` and `--muted` are. A token comment forbidding a use is binding on new code.
18. **Every animation over five seconds and every `outline: none` ships its opt-out in the same
    change** — a `prefers-reduced-motion` rule, and a ring of equal or greater visibility.
19. **Interactive targets are at least 24×24, and adjacent targets with different outcomes are never
    flush.** `.bar-x` is the sized reference.
20. **Copy names only what the UI can do and what the code actually does.** A warning must have a
    control on the page that fixes it; a sentence must not name a mechanism (a log, a scan, a setting)
    that is not the one responsible. When a doc and a screen describe one thing, they use one word.
21. **A rendered limit or protection checks its enable switch** before it is stated, and a fallback
    clause never substitutes a reassuring claim for a value that failed to load.
22. **A comment claiming a safeguard cites the code that implements it**, and a drift test asserts
    content, not shape. A dimension check is not a rasterization check.
23. **Nested interactive controls stop Enter/Space propagation** when any ancestor row or card has its
    own key handler. The `SeasonStrip` square's guard is the model; adding a control to a row without
    it is a blocker.
24. **Async work carries a generation token.** Any poll or retry loop guards against a result that
    resolves after the operation was stopped, canceled, or superseded, before it touches state or a
    handler.
25. **Never present a fallback identity as the real one.** When the item marked "current" is absent
    from a list, say so and disable the controls; never silently select index 0 of a set that addresses
    someone's server.
26. **Delete the whole supply chain.** Removing a surface removes its route, client method, query keys,
    invalidations, CSS, and comments; an uncalled client method or an unmatched selector is deleted in
    the change that orphans it.
27. **Large lists memoize.** Derived arrays are `useMemo`d, handlers are `useCallback`d, row components
    are `React.memo`d, and a per-row `useQuery` is hoisted to the container and threaded down. A query
    mounted per row is a blocker at list scale.

## Suggested fix order

1. **Batch 1, the deletion path** (each small and independently shippable): S1, B3, B4, B5, B25, PR1,
   PR10, PR8, B15, B16.
2. **Batch 2, the queue's control grammar** (one mechanical pattern, shared tests): B1, B2, B12, B24,
   B13, B36, U13.
3. **Batch 3, the number-input family** (B7 first; its dependents fall out): B7, B8, U17, PR5, PR6.
4. **Batch 4, the unknown-state and stale-cache sweep:** B9, B14, B21, B22, PR2, PR3, PR4, B20, B19,
   B18.
5. ~~**Batch 5, contrast and motion**: U1, U2, U4, U5, U6.~~ Done.
6. ~~**Batch 6, copy** (backend strings first, they need a `src/reaper` test pass): S2, S3, U3, U7,
   U8, U9, U10, U11, U12, U14, U15, U18, U19, I1.~~ Done.
7. ~~**Batch 7, refactor and performance,** by severity: R1-R9, P1-P9, H1-H3, and the remaining low
   bugs (B30-B35), I2.~~ Done, except the residue noted inline on R1 and R2.

Nothing is left in the fix order. Batch 8 then took the four unscheduled findings that mislead or
dead-end the operator (B6, B10, B11, B17), batch 9 the six that state something untrue
(B23, B26-B29, PR7), and batch 10 the four cheap hygiene fixes (S4, S5, U16, PR11). What remains
open in the document above: PR9, plus R1/R2's residue -- one build-hygiene debt with no committed
generator to hang it on, and two restructures with no behavior change.

Run `uv run ruff format .` before staging any backend change, and the full CLAUDE.md gate set before
each commit -- with `set -o pipefail`, or a failing step will hide behind the `tail` you pipe it to. When a change is observable in the app, drive it end-to-end per the `verify` skill;
B2/B13 need a real keyboard and a real pointer (B13's twin was found in Safari alone), and a narrow
viewport means a 375px `<iframe>` of the app, since Chrome will not size a window that small. In a
background session the tab is hidden, so `scroll`, `requestAnimationFrame` and IntersectionObserver
callbacks never fire on their own and have to be dispatched or flushed by hand (see batch 6).
